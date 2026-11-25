"""Стратегия ROTATION: держит рыночные позиции по списку ног и переоткрывает их раз в N минут.

Пример: BTC long, ETH short, HYPE long, SOL short — по 100$ маржи каждая, переоткрытие раз в час.
PnL не цель — стратегия для генерации объёма/тестирования testnet.

Цикл (каждые ROTATION_CHECK_SEC):
  * пора переоткрывать -> по каждой ноге: закрыть позицию reduce-only, открыть заново по рынку;
  * иначе -> проверить, что все ноги открыты в нужную сторону; недостающие добрать
    (позицию могли ликвидировать / закрыть руками / ордер мог быть отклонён).
"""
import asyncio
import json
import logging
import os
import time
from decimal import Decimal

from ..config import Config
from ..exchange import AUTH, ApiError, NetworkError, OmniClient
from ..strategy.precision import fmt, round_size
from .market import flatten_symbol, make_cid, wait_final
from .orders import FatalError

log = logging.getLogger("rotation")

MAX_CONSECUTIVE_ERRORS = 30


class RotationAuthFailure(FatalError):
    pass


class RotationState:
    def __init__(self, path: str):
        self.path = path
        self.d = {"next_rotation": 0, "halted": "", "stats": {"rotations": 0, "opens": 0, "closes": 0,
                                                                "rejects": 0, "volume_usdc": "0"}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.d.update(json.load(f))
            except Exception as e:  # noqa: BLE001
                log.error("Состояние ротации %s повреждено (%s) — начинаю заново", path, e)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.path)

    @property
    def stats(self) -> dict:
        return self.d["stats"]

    def add_volume(self, usdc: Decimal):
        self.stats["volume_usdc"] = str((Decimal(self.stats.get("volume_usdc", "0")) + usdc).quantize(Decimal("0.01")))


def rotation_state_path(cfg: Config) -> str:
    return os.path.join(cfg.data_dir, "rotation.json")


class RotationEngine:
    def __init__(self, cfg: Config, client: OmniClient):
        self.cfg = cfg
        self.c = client
        self.st = RotationState(rotation_state_path(cfg))
        self.last_beat = time.monotonic()
        self._inst: dict[str, tuple[float, dict]] = {}
        self._retry_at: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._errors = 0

    def beat(self):
        self.last_beat = time.monotonic()

    def save(self):
        if not self.cfg.dry_run:
            self.st.save()

    def cid(self) -> str:
        return make_cid(self.cfg.bot_id)

    async def instrument(self, symbol: str) -> dict:
        ts, inst = self._inst.get(symbol, (0, None))
        if inst is None or time.time() - ts > 1800:
            inst = await self.c.instrument(symbol)
            self._inst[symbol] = (time.time(), inst)
        return inst

    # ------------------------------------------------------------ планирование размера
    async def plan(self, symbol: str, side: str) -> dict:
        inst = await self.instrument(symbol)
        price = await self.c.mark_price(symbol)
        pos = await self.c.position(symbol)
        lev = Decimal(pos.get("leverage") or 0)
        if self.cfg.rotation_notional_usdc:
            notional = self.cfg.rotation_notional_usdc
        else:
            if lev <= 0:
                log.warning("%s: плечо неизвестно — считаю 1x (объём = маржа)", symbol)
                lev = Decimal(1)
            notional = self.cfg.rotation_margin_usdc * lev
        step = Decimal(inst["size_step"]) if inst.get("size_step") else Decimal(0)
        size = round_size(notional / price, step) if step else notional / price
        problem = ""
        if inst.get("close_only") or inst.get("status") not in ("open", None):
            problem = f"рынок не торгуется (status={inst.get('status')}, close_only={inst.get('close_only')})"
        elif size <= 0 or size * price < Decimal(inst.get("min_notional") or 0):
            problem = f"объём {notional} USDC меньше минимального ({inst.get('min_notional')})"
        return {"symbol": symbol, "side": side, "price": price, "lev": lev, "notional": notional, "size": size,
                "pos": Decimal(pos.get("size") or 0), "problem": problem}

    # ------------------------------------------------------------ старт
    async def start(self):
        try:
            st = await self.c.status()
        except ApiError as e:
            if e.code in AUTH:
                raise RotationAuthFailure(str(e)) from e
            raise
        acc = await self.c.account()
        log.info("API: %s | ключ %s | equity=%s доступно=%s", st.get("status"), self.c.signer.public_key,
                 acc.get("equity"), (acc.get("cross") or {}).get("available"))
        total = Decimal(0)
        for sym, side in self.cfg.rotation_legs:
            p = await self.plan(sym, side)
            total += p["notional"]
            log.info("Нога %-10s %-5s маржа %s$ × %sx = %s$ ≈ %s %s @ %s | сейчас позиция %s%s",
                     sym, side.upper(), self.cfg.rotation_margin_usdc, fmt(p["lev"]), fmt(p["notional"]),
                     fmt(p["size"]), sym.split("-")[0], p["price"], fmt(p["pos"]),
                     f" | ⚠ {p['problem']}" if p["problem"] else "")
        log.info("Ротация: %d ног, общий объём ≈ %s$, переоткрытие раз в %s мин, dry_run=%s",
                 len(self.cfg.rotation_legs), fmt(total), fmt(Decimal(str(self.cfg.rotation_interval_min))),
                 self.cfg.dry_run)
        if self.st.d.get("halted"):
            log.critical("Ротация в режиме СТОП: %s. Продолжить: make resume", self.st.d["halted"])

    # ------------------------------------------------------------ цикл
    async def run(self, stop: asyncio.Event):
        await self.start()
        while not stop.is_set():
            self.beat()
            try:
                await self.step()
                self._errors = 0
            except ApiError as e:
                if e.code in AUTH:
                    raise RotationAuthFailure(str(e)) from e
                self._on_error(e)
            except FatalError:
                raise
            except Exception as e:  # noqa: BLE001
                self._on_error(e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.rotation_check_sec * (1 + min(self._errors, 5)))
            except asyncio.TimeoutError:
                pass

    def _on_error(self, e: Exception):
        self._errors += 1
        log.error("Ошибка (%d подряд): %r", self._errors, e)
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            raise FatalError(f"{self._errors} ошибок подряд — перезапуск контейнера")

    async def step(self):
        if self.st.d.get("halted"):
            return
        if self.cfg.dry_run:
            prices = [f"{s}={await self.c.mark_price(s)}" for s, _ in self.cfg.rotation_legs]
            log.info("[DRY_RUN] %s", " ".join(prices))
            return
        now = time.time()
        if now >= self.st.d.get("next_rotation", 0):
            # время следующей ротации ставим ДО работы: сбой посреди ротации не вызовет цикл переоткрытий
            self.st.d["next_rotation"] = now + self.cfg.rotation_interval_min * 60
            self.save()
            await self.rotate_all()
        else:
            await self.ensure_legs()

    async def rotate_all(self):
        self.st.stats["rotations"] = self.st.stats.get("rotations", 0) + 1
        n = self.st.stats["rotations"]
        log.info("=== РОТАЦИЯ #%d: закрываю и открываю заново %d ног ===", n, len(self.cfg.rotation_legs))
        for sym, side in self.cfg.rotation_legs:
            self.beat()
            await self.close_leg(sym)
            await self.open_leg(sym, side)
        nxt = time.strftime("%H:%M:%S", time.localtime(self.st.d["next_rotation"]))
        log.info("=== РОТАЦИЯ #%d готова. Следующая в %s | %s ===", n, nxt, self.st.stats)

    async def ensure_legs(self):
        for sym, side in self.cfg.rotation_legs:
            if time.time() < self._retry_at.get(sym, 0):
                continue
            pos = Decimal((await self.c.position(sym)).get("size") or 0)
            want = 1 if side == "long" else -1
            if pos * want > 0:
                continue
            if pos != 0:
                log.warning("%s: позиция %s не в ту сторону (нужен %s) — переворачиваю", sym, pos, side)
                await self.close_leg(sym)
            else:
                log.warning("%s: позиции нет (нужен %s) — открываю", sym, side)
            await self.open_leg(sym, side)

    async def close_leg(self, sym: str):
        pos = await self.c.position(sym)
        size = Decimal(pos.get("size") or 0)
        if size == 0:
            return
        ok = await flatten_symbol(self.c, sym, self.cfg.rotation_max_slippage * 2, self.cid)
        if ok:
            self.st.stats["closes"] = self.st.stats.get("closes", 0) + 1
            self.st.add_volume(abs(size) * Decimal(pos.get("mark_price") or 0))
            self.save()

    async def open_leg(self, sym: str, side: str, attempts: int = 3) -> bool:
        p = await self.plan(sym, side)
        if p["problem"]:
            log.error("%s: не открываю — %s", sym, p["problem"])
            self._fail(sym)
            return False
        for i in range(attempts):
            slip = min(self.cfg.rotation_max_slippage * (1 + i), Decimal("0.2"))
            body = {"type": "market", "symbol": sym, "side": "buy" if side == "long" else "sell",
                    "size": fmt(p["size"]), "max_slippage": fmt(slip), "client_order_id": self.cid()}
            try:
                o = await self.c.place_order(body)
                o = await wait_final(self.c, o["order_id"])
            except (ApiError, NetworkError) as e:
                if isinstance(e, ApiError) and e.code in AUTH:
                    raise
                log.warning("%s: ошибка открытия (%d/%d): %s", sym, i + 1, attempts, e)
                await asyncio.sleep(5 * (i + 1))
                continue
            if o.get("status") == "filled":
                fill = Decimal(o.get("fill_price") or p["price"])
                self.st.stats["opens"] = self.st.stats.get("opens", 0) + 1
                self.st.add_volume(p["size"] * fill)
                self._fails.pop(sym, None)
                self._retry_at.pop(sym, None)
                self.save()
                log.info("ОТКРЫТ %s %s %s @ %s (≈%s$, маржа %s$ × %sx)", sym, side.upper(), fmt(p["size"]), fill,
                         (p["size"] * fill).quantize(Decimal("1")), self.cfg.rotation_margin_usdc, fmt(p["lev"]))
                return True
            self.st.stats["rejects"] = self.st.stats.get("rejects", 0) + 1
            log.warning("%s: ордер %s (%s) — попытка %d/%d", sym, o.get("status"), o.get("reason"), i + 1, attempts)
            await asyncio.sleep(5 * (i + 1))
        self._fail(sym)
        return False

    def _fail(self, sym: str):
        n = self._fails.get(sym, 0) + 1
        self._fails[sym] = n
        wait = min(60 * 2 ** (n - 1), 900)
        self._retry_at[sym] = time.time() + wait
        log.warning("%s: не удалось открыть, повтор через %ds", sym, wait)

    # ------------------------------------------------------------ ручные команды
    async def close_all(self, halt: bool = True) -> bool:
        ok = True
        for sym, _ in self.cfg.rotation_legs:
            ok = await flatten_symbol(self.c, sym, self.cfg.rotation_max_slippage * 2, self.cid) and ok
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
