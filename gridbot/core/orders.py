"""Менеджер ордеров: размещение, сверка с биржей, обработка отказов, отмена, закрытие позиции."""
import asyncio
import logging
import secrets
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from ..config import Config
from ..exchange import ApiError, NetworkError, OmniClient
from ..exchange.errors import PLACE_ERRORS, REJECT_BACKOFF
from ..strategy.grid import Slot, counter_slot
from ..strategy.precision import fmt, round_price, round_size
from .state import BotState, PendingSlot, TrackedOrder

log = logging.getLogger("orders")

MAX_BACKOFF = 1800.0
LIVE = ("open", "queued", "untriggered")


class FatalError(Exception):
    """Бот не может продолжать без вмешательства человека."""


class OrderManager:
    def __init__(self, cfg: Config, client: OmniClient, state: BotState, levels: list[Decimal], save):
        self.cfg = cfg
        self.c = client
        self.st = state
        self.levels = levels
        self.save = save
        self.inst: dict = {}
        self._inst_ts = 0.0
        self.size = Decimal(0)

    # ------------------------------------------------------------ instrument
    async def refresh_instrument(self, force: bool = False):
        if not force and time.time() - self._inst_ts < 1800:
            return
        self.inst = await self.c.instrument(self.cfg.symbol)
        self._inst_ts = time.time()
        self._recalc_size()

    def _recalc_size(self):
        if not self.st.base_size:
            return
        base = Decimal(self.st.base_size)
        step = Decimal(self.inst["size_step"]) if self.inst.get("size_step") else Decimal(0)
        size = round_size(base, step) if step else base
        if size <= 0:
            raise FatalError(f"размер ордера {base} меньше size_step {step} — увеличь ORDER_SIZE/GRID_BALANCE_PCT")
        min_notional = Decimal(self.inst.get("min_notional") or 0)
        if size * self.levels[0] < min_notional:
            raise FatalError(f"ордер {size} × {self.levels[0]} < min_notional {min_notional} USDC")
        if size != self.size:
            log.info("Размер ордера: %s (size_step=%s)", fmt(size), step)
        self.size = size

    def market_open(self) -> bool:
        return self.inst.get("status") in ("open", None) and not self.inst.get("close_only")

    def price_for(self, slot: Slot) -> Decimal:
        rounding = ROUND_DOWN if slot.side == "buy" else ROUND_UP
        return round_price(self.levels[slot.level], self.inst["price_precision"], rounding)

    def _next_cid(self) -> str:
        # уникален навсегда (требование API), не зависит от состояния: bot-ms-random
        self.st.seq += 1
        return f"{self.cfg.bot_id}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"

    # ------------------------------------------------------------ reconcile
    async def reconcile(self):
        """Сверка: что исполнилось / отклонилось / отменено вне бота."""
        open_ids = {o["order_id"] for o in await self.c.open_orders(self.cfg.symbol)}
        for oid in [k for k in self.st.orders if k not in open_ids]:
            await self._resolve(oid)

    async def _resolve(self, oid: str):
        tr = self.st.orders[oid]
        try:
            o = await self.c.order(oid)
        except ApiError as e:
            if e.code in ("not_found", "order_not_found"):
                log.warning("Ордер %s не найден на бирже — переставлю уровень L%d", oid, tr.level)
                del self.st.orders[oid]
                self._enqueue(Slot(tr.level, tr.side), counter=tr.counter)
                return
            raise
        status = o.get("status")
        if status in LIVE:
            return
        del self.st.orders[oid]
        slot = Slot(tr.level, tr.side)
        if status == "filled":
            self._on_fill(slot, o, tr)
        elif status == "rejected":
            reason = o.get("reason") or "unknown"
            self.st.stats["rejects"] = self.st.stats.get("rejects", 0) + 1
            log.warning("ОТКАЗ биржи: %s L%d @ %s — %s %s", slot.side, slot.level, tr.price, reason, o.get("detail") or "")
            self._enqueue(slot, error=f"rejected:{reason}", base=REJECT_BACKOFF.get(reason, 60), counter=tr.counter)
            if reason == "invalid_order":
                self._inst_ts = 0  # перечитать правила инструмента
        else:  # canceled
            reason = o.get("reason") or "unknown"
            log.warning("Ордер %s L%d отменён (%s) — переставлю", slot.side, slot.level, reason)
            self._enqueue(slot, error=f"canceled:{reason}", base=5 if reason == "user_canceled" else 60,
                          counter=tr.counter)
        self.save()

    def _on_fill(self, slot: Slot, o: dict, tr: TrackedOrder):
        s = self.st.stats
        s["buys" if slot.side == "buy" else "sells"] = s.get("buys" if slot.side == "buy" else "sells", 0) + 1
        if tr.counter:  # встречный ордер исполнен -> цикл закрыт, заработан один шаг сетки
            s["round_trips"] = s.get("round_trips", 0) + 1
            step = self.levels[1] - self.levels[0]
            gain = step * Decimal(o.get("size") or tr.size)
            s["grid_profit"] = str((Decimal(s.get("grid_profit", "0")) + gain).quantize(Decimal("0.0001")))
        log.info("ИСПОЛНЕН %s L%d %s @ %s | сделок buy/sell %s/%s, циклов %s, профит сетки ≈ %s USDC",
                 slot.side.upper(), slot.level, o.get("size"), o.get("fill_price"),
                 s.get("buys"), s.get("sells"), s.get("round_trips"), s.get("grid_profit"))
        nxt = counter_slot(slot, len(self.levels))
        if nxt:
            self._enqueue(nxt, counter=True)

    def _enqueue(self, slot: Slot, error: str = "", base: float = 0, counter: bool = False):
        # не дублируем уже ожидающий слот на том же уровне
        for p in self.st.pending:
            if p.level == slot.level:
                p.side = slot.side
                p.counter = counter
                if error:
                    p.attempts += 1
                    p.last_error = error
                    p.not_before = time.time() + min(base * 2 ** (p.attempts - 1), MAX_BACKOFF)
                return
        p = PendingSlot(slot.level, slot.side, counter=counter)
        if error:
            p.attempts, p.last_error, p.not_before = 1, error, time.time() + base
        self.st.pending.append(p)

    # ------------------------------------------------------------ placing
    async def place_pending(self):
        if not self.st.pending:
            return
        if not self.market_open():
            log.info("Рынок %s не торгуется (status=%s) — жду", self.cfg.symbol, self.inst.get("status"))
            return
        now = time.time()
        busy = self.st.busy_levels()
        # ближние к цене уровни — первыми (важнее, если бюджет запросов ограничен)
        for p in sorted(list(self.st.pending), key=lambda p: p.not_before):
            if p.not_before > now:
                continue
            if p.level in busy:
                self.st.pending.remove(p)
                continue
            ok = await self._place_slot(p)
            if ok:
                self.st.pending.remove(p)
                busy.add(p.level)
            self.save()

    async def _place_slot(self, p: PendingSlot) -> bool:
        slot = Slot(p.level, p.side)
        body = {
            "type": "limit",
            "symbol": self.cfg.symbol,
            "side": slot.side,
            "size": fmt(self.size),
            "price": fmt(self.price_for(slot)),
            "trigger_by": self.cfg.trigger_by,
            "max_slippage": fmt(self.cfg.max_slippage) if self.cfg.max_slippage is not None else None,
            "client_order_id": self._next_cid(),
        }
        try:
            o = await self.c.place_order(body)
        except ApiError as e:
            wait, refresh = PLACE_ERRORS.get(e.code, (120, False))
            self._bump(p, f"http:{e.code}", wait)
            log.warning("Не удалось поставить %s L%d @ %s: %s %s — повтор через %ds", slot.side, slot.level,
                        body["price"], e.code, e.body.get("error_fields") or e.message, p.not_before - time.time())
            if refresh:
                self._inst_ts = 0
            return False
        except NetworkError as e:
            self._bump(p, "network", 10)
            log.warning("Сеть при размещении %s L%d: %s", slot.side, slot.level, e)
            return False

        if o.get("status") in ("rejected", "canceled"):
            reason = o.get("reason") or "unknown"
            self._bump(p, f"{o['status']}:{reason}", REJECT_BACKOFF.get(reason, 60))
            log.warning("Ордер %s L%d сразу %s: %s", slot.side, slot.level, o["status"], reason)
            return False
        self.st.orders[o["order_id"]] = TrackedOrder(slot.level, slot.side, body["client_order_id"],
                                                     body["price"], body["size"], p.counter)
        log.info("Поставлен %s L%d %s @ %s (%s)", slot.side.upper(), slot.level, body["size"], body["price"],
                 o.get("status"))
        if o.get("status") == "filled":  # лимитка через рынок исполнилась сразу
            await self._resolve(o["order_id"])
        return True

    @staticmethod
    def _bump(p: PendingSlot, err: str, base: float):
        p.attempts += 1
        p.last_error = err
        p.not_before = time.time() + min(base * 2 ** (p.attempts - 1), MAX_BACKOFF)

    # ------------------------------------------------------------ bulk ops
    async def cancel_all(self) -> int:
        canceled = await self.c.cancel_all(self.cfg.symbol)
        # исполнившиеся в момент отмены учтём при следующей сверке — поэтому только отменённые убираем
        for o in canceled:
            self.st.orders.pop(o.get("order_id"), None)
        open_left = {o["order_id"] for o in await self.c.open_orders(self.cfg.symbol)}
        for oid in [k for k in self.st.orders if k not in open_left]:
            await self._resolve(oid)  # исполнившиеся до отмены попадут в статистику
        self.st.pending.clear()
        self.save()
        log.info("Отменено ордеров: %d (осталось в исполнении: %d)", len(canceled), len(open_left))
        return len(canceled)

    async def flatten(self, attempts: int = 6) -> bool:
        """Закрыть позицию по рынку reduce-only. True — позиция ноль."""
        for i in range(attempts):
            pos = await self.c.position(self.cfg.symbol)
            size = Decimal(pos.get("size") or "0")
            if size == 0:
                log.info("Позиция %s закрыта", self.cfg.symbol)
                return True
            side = "sell" if size > 0 else "buy"
            slip = min(self.cfg.close_max_slippage * (1 + i), Decimal("0.2"))  # расширяем с каждой попыткой
            body = {"type": "market", "symbol": self.cfg.symbol, "side": side, "size": fmt(abs(size)),
                    "max_slippage": fmt(slip), "reduce_only": True, "client_order_id": self._next_cid()}
            log.warning("ЗАКРЫТИЕ позиции %s: %s %s по рынку (slippage до %s)", size, side, fmt(abs(size)), slip)
            try:
                o = await self.c.place_order(body, critical=True)
                o = await self._wait_final(o["order_id"])
                log.info("Закрывающий ордер: %s %s", o.get("status"), o.get("reason") or o.get("fill_price"))
                if o.get("status") == "filled":
                    continue  # перепроверим позицию сразу
            except (ApiError, NetworkError) as e:
                log.error("Закрытие не удалось: %s", e)
            await asyncio.sleep(min(3 * (i + 1), 15))
        log.critical("Не удалось закрыть позицию за %d попыток — закрой вручную!", attempts)
        return False

    async def _wait_final(self, oid: str, timeout: float = 30) -> dict:
        end = time.time() + timeout
        o = await self.c.order(oid)
        while o.get("status") in LIVE and time.time() < end:
            await asyncio.sleep(2)
            o = await self.c.order(oid)
        return o
