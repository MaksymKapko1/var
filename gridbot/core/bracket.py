"""Стратегия BRACKET: трендовый скальпер с лимитным входом и прикреплёнными TP/SL.

Для каждого символа (по умолчанию US500-PERP и XAU-PERP) — конечный автомат:

  FLAT      сигнал = быстрая EMA vs медленная EMA (по mark price, сэмпл раз в BRACKET_POLL_SEC);
            ставим лимитку по тренду на BRACKET_ENTRY_OFFSET_BPS лучше mark,
            сразу с take-profit (+TP%) и stop-loss (−SL%) — одним запросом (Omni attached TP/SL)
  ENTRY     не исполнилась за BRACKET_ENTRY_TTL_SEC -> отменяем и переставляем по новой цене
  POSITION  позицию закрывает TP или SL; держим не дольше BRACKET_MAX_HOLD_MIN -> закрываем по рынку;
            за BRACKET_SESSION_GUARD_MIN до закрытия сессии TradFi-рынка — закрываем и не входим
  -> FLAT + пауза BRACKET_COOLDOWN_SEC

Ошибки одного символа не останавливают другой; отказы — с экспоненциальной паузой.
"""
import asyncio
import json
import logging
import os
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from ..config import Config
from ..exchange import AUTH, ApiError, NetworkError, OmniClient
from ..exchange.errors import PLACE_ERRORS, REJECT_BACKOFF
from ..strategy.precision import fmt, round_price, round_size
from .market import LIVE, flatten_symbol, make_cid
from .orders import FatalError

log = logging.getLogger("bracket")

MAX_CONSECUTIVE_ERRORS = 30
INST_TTL = 120


class BracketAuthFailure(FatalError):
    pass


def bracket_state_path(cfg: Config) -> str:
    return os.path.join(cfg.data_dir, "bracket.json")


class Ema:
    def __init__(self, n: int):
        self.k = Decimal(2) / Decimal(n + 1)
        self.v: Decimal | None = None

    def update(self, x: Decimal) -> Decimal:
        self.v = x if self.v is None else self.v + self.k * (x - self.v)
        return self.v


class BracketState:
    def __init__(self, path: str):
        self.path = path
        self.d = {"halted": "", "symbols": {},
                  "stats": {"entries": 0, "reprices": 0, "fills": 0, "tp": 0, "sl": 0, "timeouts": 0,
                            "session_closes": 0, "rejects": 0, "volume_usdc": "0", "pnl_est_usdc": "0"}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.d.update(json.load(f))
            except Exception as e:  # noqa: BLE001
                log.error("Состояние %s повреждено (%s) — начинаю заново", path, e)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    @property
    def stats(self) -> dict:
        return self.d["stats"]

    def sym(self, s: str) -> dict:
        return self.d["symbols"].setdefault(s, {"phase": "flat"})

    def inc(self, key: str, n: int = 1):
        self.stats[key] = self.stats.get(key, 0) + n

    def add(self, key: str, v: Decimal):
        self.stats[key] = str((Decimal(self.stats.get(key, "0")) + v).quantize(Decimal("0.01")))


class BracketEngine:
    def __init__(self, cfg: Config, client: OmniClient):
        self.cfg = cfg
        self.c = client
        self.st = BracketState(bracket_state_path(cfg))
        self.last_beat = time.monotonic()
        self.fast = {s: Ema(cfg.bracket_fast) for s in cfg.bracket_symbols}
        self.slow = {s: Ema(cfg.bracket_slow) for s in cfg.bracket_symbols}
        self.samples = {s: 0 for s in cfg.bracket_symbols}
        self._inst: dict[str, tuple[float, dict]] = {}
        self._errors = 0
        self._last_closed_log: dict[str, float] = {}

    # ---------------------------------------------------------------- utils
    def beat(self):
        self.last_beat = time.monotonic()

    def save(self):
        if not self.cfg.dry_run:
            self.st.save()

    def cid(self) -> str:
        return make_cid(self.cfg.bot_id)

    async def instrument(self, s: str) -> dict:
        ts, inst = self._inst.get(s, (0, None))
        if inst is None or time.time() - ts > INST_TTL:
            inst = await self.c.instrument(s)
            self._inst[s] = (time.time(), inst)
        return inst

    @staticmethod
    def market_open(inst: dict) -> bool:
        return inst.get("status") in ("open", None) and not inst.get("close_only")

    def closing_soon(self, inst: dict) -> bool:
        nc = inst.get("next_close_at")
        return bool(inst.get("status") == "open" and nc and nc / 1000 - time.time() < self.cfg.bracket_session_guard_min * 60)

    def signal(self, s: str) -> str | None:
        if self.samples[s] < self.cfg.bracket_min_samples:
            return None
        return "long" if self.fast[s].v >= self.slow[s].v else "short"

    async def size_for(self, s: str, price: Decimal, inst: dict) -> tuple[Decimal, Decimal, str]:
        if self.cfg.bracket_notional_usdc:
            notional, lev = self.cfg.bracket_notional_usdc, Decimal(1)
        else:
            lev = Decimal((await self.c.position(s)).get("leverage") or 0)
            if lev <= 0:
                lev = Decimal(1)
            notional = self.cfg.bracket_margin_usdc * lev
        step = Decimal(inst["size_step"]) if inst.get("size_step") else Decimal(0)
        size = round_size(notional / price, step) if step else notional / price
        problem = ""
        if size <= 0 or size * price < Decimal(inst.get("min_notional") or 0):
            problem = f"объём {fmt(notional)}$ меньше минимального {inst.get('min_notional')}$"
        return size, lev, problem

    # ---------------------------------------------------------------- start
    async def start(self):
        try:
            st = await self.c.status()
        except ApiError as e:
            if e.code in AUTH:
                raise BracketAuthFailure(str(e)) from e
            raise
        acc = await self.c.account()
        log.info("API: %s | ключ %s | equity=%s доступно=%s", st.get("status"), self.c.signer.public_key,
                 acc.get("equity"), (acc.get("cross") or {}).get("available"))
        c = self.cfg
        log.info("Bracket: TP +%s%% / SL −%s%% | вход на %s б.п. лучше mark, переставка через %ss | макс. удержание "
                 "%s мин | EMA %s/%s по %ss | dry_run=%s", c.bracket_tp_pct, c.bracket_sl_pct,
                 c.bracket_entry_offset_bps, int(c.bracket_entry_ttl_sec), int(c.bracket_max_hold_min),
                 c.bracket_fast, c.bracket_slow, int(c.bracket_poll_sec), c.dry_run)
        for s in c.bracket_symbols:
            inst = await self.instrument(s)
            price = await self.c.mark_price(s)
            size, lev, problem = await self.size_for(s, price, inst)
            pos = Decimal((await self.c.position(s)).get("size") or 0)
            nc = inst.get("next_close_at")
            no = inst.get("next_open_at")
            log.info("%-11s status=%s | цена %s | сделка %s ≈ %s$ (маржа %s$ × %sx) | позиция %s | закрытие сессии %s"
                     " | открытие %s%s", s, inst.get("status"), price, fmt(size), (size * price).quantize(Decimal(1)),
                     c.bracket_margin_usdc, fmt(lev), fmt(pos), _ts(nc), _ts(no),
                     f" | ⚠ {problem}" if problem else "")
            if c.dry_run:
                continue
            ss = self.st.sym(s)
            if ss.get("phase", "flat") == "flat":
                # чистый старт по символу: снять хвосты, закрыть «чужую» позицию
                await self.c.cancel_all(s)
                if pos != 0 and self.market_open(inst):
                    log.warning("%s: позиция %s без сопровождения — закрываю", s, pos)
                    await flatten_symbol(self.c, s, Decimal("0.01"), self.cid)
            else:
                log.info("%s: продолжаю с фазы %s", s, ss["phase"])
        if self.st.d.get("halted"):
            log.critical("Bracket в режиме СТОП: %s. Продолжить: make resume", self.st.d["halted"])
        self.save()

    # ---------------------------------------------------------------- loop
    async def run(self, stop: asyncio.Event):
        await self.start()
        while not stop.is_set():
            self.beat()
            failed = 0
            for s in self.cfg.bracket_symbols:
                if stop.is_set():
                    break
                try:
                    await self.process(s)
                except ApiError as e:
                    if e.code in AUTH:
                        raise BracketAuthFailure(str(e)) from e
                    failed += 1
                    log.error("%s: ошибка API: %s", s, e)
                except FatalError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log.error("%s: ошибка: %r", s, e)
                self.beat()
            self._errors = self._errors + 1 if failed == len(self.cfg.bracket_symbols) else 0
            if self._errors >= MAX_CONSECUTIVE_ERRORS:
                raise FatalError(f"{self._errors} циклов подряд с ошибками — перезапуск контейнера")
            self.save()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.bracket_poll_sec * (1 + min(self._errors, 5)))
            except asyncio.TimeoutError:
                pass

    async def process(self, s: str):
        inst = await self.instrument(s)
        price = await self.c.mark_price(s)
        f, sl = self.fast[s].update(price), self.slow[s].update(price)
        self.samples[s] += 1
        if self.cfg.dry_run:
            if self.samples[s] % 6 == 1:
                log.info("[DRY_RUN] %s %s | EMA %s/%s -> %s", s, price, f.quantize(Decimal("0.01")),
                         sl.quantize(Decimal("0.01")), self.signal(s) or "прогрев")
            return
        if self.st.d.get("halted"):
            return
        ss = self.st.sym(s)
        phase = ss.get("phase", "flat")
        if phase == "flat":
            await self._flat(s, ss, inst, price)
        elif phase == "entry":
            await self._entry(s, ss, inst)
        elif phase == "position":
            await self._position(s, ss, inst)

    # ---------------------------------------------------------------- phases
    async def _flat(self, s: str, ss: dict, inst: dict, price: Decimal):
        now = time.time()
        if not self.market_open(inst) or self.closing_soon(inst):
            if now - self._last_closed_log.get(s, 0) > 900:
                self._last_closed_log[s] = now
                log.info("%s: рынок %s (открытие %s) — жду", s,
                         "скоро закроется" if self.closing_soon(inst) else inst.get("status"), _ts(inst.get("next_open_at")))
            return
        if now < ss.get("cooldown_until", 0) or now < ss.get("retry_at", 0):
            return
        side = self.signal(s)
        if side is None:
            return
        await self._place_entry(s, ss, inst, price, side)

    async def _place_entry(self, s: str, ss: dict, inst: dict, price: Decimal, side: str):
        size, lev, problem = await self.size_for(s, price, inst)
        if problem:
            log.error("%s: не вхожу — %s", s, problem)
            self._fail(ss, 600)
            return
        prec = inst["price_precision"]
        off = self.cfg.bracket_entry_offset_bps / 10000
        tp, slp = self.cfg.bracket_tp_pct / 100, self.cfg.bracket_sl_pct / 100
        if side == "long":
            px = round_price(price * (1 - off), prec, ROUND_DOWN)
            tp_px, sl_px = round_price(px * (1 + tp), prec, ROUND_UP), round_price(px * (1 - slp), prec, ROUND_DOWN)
        else:
            px = round_price(price * (1 + off), prec, ROUND_UP)
            tp_px, sl_px = round_price(px * (1 - tp), prec, ROUND_DOWN), round_price(px * (1 + slp), prec, ROUND_UP)
        body = {
            "type": "limit", "symbol": s, "side": "buy" if side == "long" else "sell", "size": fmt(size),
            "price": fmt(px), "trigger_by": "quote", "max_slippage": "0.01", "client_order_id": self.cid(),
            "take_profit": {"trigger_price": fmt(tp_px), "trigger_by": "mark", "max_slippage": None},
            "stop_loss": {"trigger_price": fmt(sl_px), "trigger_by": "mark", "max_slippage": None},
        }
        try:
            placed = await self.c.place_orders(body)
        except ApiError as e:
            wait = PLACE_ERRORS.get(e.code, (120, False))[0]
            if e.code == "conflicting_order":
                await self.c.cancel_all(s)
                wait = 5
            self.st.inc("rejects")
            log.warning("%s: вход не принят: %s %s — пауза %ss", s, e.code, e.body.get("error_fields") or e.message, int(wait))
            self._fail(ss, wait)
            return
        parent = placed[0]
        kids = {o.get("type"): o.get("order_id") for o in placed[1:]}
        ss.update(phase="entry", side=side, entry_id=parent["order_id"], tp_id=kids.get("take_profit"),
                  sl_id=kids.get("stop_loss"), placed_at=time.time(), size=fmt(size), entry_px=fmt(px),
                  tp_px=fmt(tp_px), sl_px=fmt(sl_px))
        self.st.inc("entries")
        log.info("%s: ВХОД %s %s @ %s (mark %s) | TP %s | SL %s", s, side.upper(), fmt(size), fmt(px), price,
                 fmt(tp_px), fmt(sl_px))
        if parent.get("status") in ("filled", "rejected", "canceled"):
            await self._entry(s, ss, inst)

    async def _entry(self, s: str, ss: dict, inst: dict):
        o = await self.c.order(ss["entry_id"])
        st = o.get("status")
        if st in LIVE:
            age = time.time() - ss.get("placed_at", 0)
            if age < self.cfg.bracket_entry_ttl_sec and self.market_open(inst) and not self.closing_soon(inst):
                return
            if st == "queued":  # уже исполняется — не трогаем
                return
            try:
                o = await self.c.cancel_order(ss["entry_id"])
            except ApiError as e:
                if e.code not in ("order_filled", "order_queued", "order_rejected", "order_not_found"):
                    raise
                o = await self.c.order(ss["entry_id"])
            st = o.get("status")
            if st == "canceled":
                self.st.inc("reprices")
                log.info("%s: вход не исполнился за %ds — переставляю", s, int(age))
                ss.update(phase="flat")
                return
        if st == "filled":
            fill = Decimal(o.get("fill_price") or ss["entry_px"])
            ss.update(phase="position", opened_at=time.time(), entry_fill=str(fill))
            self.st.inc("fills")
            self.st.add("volume_usdc", Decimal(ss["size"]) * fill)
            log.info("%s: ИСПОЛНЕН вход %s %s @ %s — жду TP %s / SL %s", s, ss["side"].upper(), ss["size"], fill,
                     ss["tp_px"], ss["sl_px"])
        elif st == "rejected":
            reason = o.get("reason") or "unknown"
            self.st.inc("rejects")
            log.warning("%s: вход отклонён биржей: %s", s, reason)
            await self.c.cancel_all(s)
            ss.update(phase="flat")
            self._fail(ss, REJECT_BACKOFF.get(reason, 60))
        elif st == "canceled":
            ss.update(phase="flat")

    async def _position(self, s: str, ss: dict, inst: dict):
        pos = await self.c.position(s)
        size = Decimal(pos.get("size") or 0)
        if size == 0:
            outcome, exit_px = await self._exit_kind(ss)
            self.st.inc(outcome)
            entry = Decimal(ss.get("entry_fill") or ss["entry_px"])
            if exit_px:
                sign = 1 if ss["side"] == "long" else -1
                pnl = (exit_px - entry) * Decimal(ss["size"]) * sign
                self.st.add("pnl_est_usdc", pnl)
                self.st.add("volume_usdc", Decimal(ss["size"]) * exit_px)
            log.info("%s: ВЫХОД по %s @ %s | %s", s, outcome.upper(), exit_px or "?", self._stats_line())
            await self.c.cancel_all(s)  # снять оставшийся TP/SL
            self._to_flat(ss)
            return
        age_min = (time.time() - ss.get("opened_at", time.time())) / 60
        reason = ("timeouts" if age_min >= self.cfg.bracket_max_hold_min else
                  "session_closes" if self.closing_soon(inst) else None)
        if reason and self.market_open(inst):
            log.info("%s: закрываю по рынку (%s, держим %.0f мин)", s,
                     "время удержания вышло" if reason == "timeouts" else "скоро закрытие сессии", age_min)
            await self.c.cancel_all(s)
            if await flatten_symbol(self.c, s, Decimal("0.01"), self.cid):
                self.st.inc(reason)
                self.st.add("volume_usdc", abs(size) * Decimal(pos.get("mark_price") or ss["entry_px"]))
                self._to_flat(ss)

    async def _exit_kind(self, ss: dict) -> tuple[str, Decimal | None]:
        for key, name in (("tp_id", "tp"), ("sl_id", "sl")):
            if ss.get(key):
                try:
                    o = await self.c.order(ss[key])
                except ApiError:
                    continue
                if o.get("status") == "filled":
                    return name, Decimal(o["fill_price"]) if o.get("fill_price") else None
        return "timeouts", None

    def _to_flat(self, ss: dict):
        for k in ("entry_id", "tp_id", "sl_id", "placed_at", "opened_at", "entry_fill"):
            ss.pop(k, None)
        ss.update(phase="flat", cooldown_until=time.time() + self.cfg.bracket_cooldown_sec, fails=0)

    def _fail(self, ss: dict, base: float):
        n = ss.get("fails", 0) + 1
        ss["fails"] = n
        ss["retry_at"] = time.time() + min(base * 2 ** (n - 1), 1800)

    def _stats_line(self) -> str:
        s = self.st.stats
        return (f"входов {s.get('entries')} (переставлено {s.get('reprices')}), сделок {s.get('fills')}, "
                f"TP {s.get('tp')} / SL {s.get('sl')} / по времени {s.get('timeouts')}, "
                f"объём {s.get('volume_usdc')}$, PnL≈{s.get('pnl_est_usdc')}$")

    # ---------------------------------------------------------------- manual
    async def close_all(self, halt: bool = True) -> bool:
        ok = True
        for s in self.cfg.bracket_symbols:
            await self.c.cancel_all(s)
            ok = await flatten_symbol(self.c, s, Decimal("0.01"), self.cid) and ok
            self.st.sym(s).update(phase="flat")
        if halt:
            self.st.d["halted"] = "закрыто вручную (close)"
        self.st.save()
        return ok

    async def shutdown(self):
        if self.cfg.close_on_exit and not self.cfg.dry_run:
            try:
                await self.close_all(halt=False)
            except Exception as e:  # noqa: BLE001
                log.error("Ошибка закрытия при остановке: %r", e)


def _ts(ms) -> str:
    if not ms:
        return "—"
    return time.strftime("%d.%m %H:%M UTC", time.gmtime(ms / 1000))
