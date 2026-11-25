"""Грид-бот: размещает лимитки по сетке, следит за исполнением, ставит встречные ордера."""
import json
import logging
import os
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from .api import ApiError, OmniClient
from .config import Config
from .grid import Slot, counter_slot, initial_slots, make_levels
from .precision import fmt, round_price, round_size

log = logging.getLogger("bot")

REJECT_RETRY_SEC = 60  # не долбить API, если ордер отклонён


class GridBot:
    def __init__(self, cfg: Config, client: OmniClient):
        self.cfg = cfg
        self.c = client
        self.levels = make_levels(cfg.grid_lower, cfg.grid_upper, cfg.grid_levels)
        self.inst: dict = {}
        self.inst_ts = 0.0
        self.base_size: Decimal | None = cfg.order_size  # до округления под size_step
        self.size = cfg.order_size or Decimal(0)
        self.seq = 0
        # state
        self.orders: dict[str, dict] = {}  # order_id -> {level, side, cid, price}
        self.pending: list[dict] = []  # [{level, side, not_before}]
        self.stats = {"buys": 0, "sells": 0, "round_trips": 0, "grid_profit": "0"}

    # ---------------- state ----------------
    def _params(self) -> dict:
        sizing = f"pct:{self.cfg.balance_pct}" if self.cfg.balance_pct is not None else f"size:{self.cfg.order_size}"
        return {"symbol": self.cfg.symbol, "levels": [str(x) for x in self.levels], "sizing": sizing}

    def load_state(self) -> bool:
        f = self.cfg.state_file
        if not f or not os.path.exists(f):
            return False
        try:
            with open(f) as fh:
                data = json.load(fh)
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось прочитать состояние %s: %s", f, e)
            return False
        if data.get("params") != self._params():
            log.warning("Параметры сетки изменились — старое состояние игнорирую")
            return False
        self.orders = data.get("orders", {})
        self.pending = data.get("pending", [])
        self.stats.update(data.get("stats", {}))
        if data.get("base_size"):
            self.base_size = Decimal(data["base_size"])
        return bool(self.orders or self.pending)

    def save_state(self):
        f = self.cfg.state_file
        if not f:
            return
        os.makedirs(os.path.dirname(f) or ".", exist_ok=True)
        tmp = f + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"params": self._params(), "orders": self.orders, "pending": self.pending,
                       "stats": self.stats, "base_size": str(self.base_size) if self.base_size else None}, fh, indent=1)
        os.replace(tmp, f)

    # ---------------- instrument ----------------
    def refresh_instrument(self, force: bool = False):
        if not force and time.time() - self.inst_ts < 1800:
            return
        self.inst = self.c.instrument(self.cfg.symbol)
        self.inst_ts = time.time()
        step = Decimal(self.inst["size_step"]) if self.inst.get("size_step") else Decimal(0)
        if self.base_size is None:
            return  # размер ещё не посчитан из баланса (см. startup)
        size = round_size(self.base_size, step) if step else self.base_size
        if size <= 0:
            raise SystemExit(f"[config] размер ордера {self.base_size} меньше size_step {step}")
        if size != self.size:
            log.info("Размер ордера округлён под size_step=%s: %s", step, size)
        self.size = size
        min_notional = Decimal(self.inst["min_notional"])
        if size * self.levels[0] < min_notional:
            raise SystemExit(f"[config] ордер {size} x {self.levels[0]} = {size * self.levels[0]} USDC < min_notional {min_notional}")

    def size_from_balance(self, equity: Decimal) -> Decimal:
        """GRID_BALANCE_PCT: % equity = суммарный объём (notional) всех ордеров сетки."""
        budget = equity * self.cfg.balance_pct / 100
        per_order = budget / (len(self.levels) - 1)
        mid = (self.levels[0] + self.levels[-1]) / 2
        size = per_order / mid
        log.info("Размер из баланса: equity=%s × %s%% = %s USDC на сетку -> %s USDC на ордер -> %s %s",
                 equity, self.cfg.balance_pct, budget.quantize(Decimal("0.01")), per_order.quantize(Decimal("0.01")),
                 size.quantize(Decimal("0.00000001")), self.cfg.symbol.split("-")[0])
        return size

    def price_for(self, slot: Slot) -> Decimal:
        rounding = ROUND_DOWN if slot.side == "buy" else ROUND_UP
        return round_price(self.levels[slot.level], self.inst["price_precision"], rounding)

    # ---------------- lifecycle ----------------
    def startup(self):
        st = self.c.status()
        log.info("API status: %s", st.get("status"))
        self.refresh_instrument(force=True)
        i = self.inst
        log.info("Инструмент %s: status=%s close_only=%s min_notional=%s size_step=%s price_precision=%s",
                 i["symbol"], i.get("status"), i.get("close_only"), i.get("min_notional"),
                 i.get("size_step"), i.get("price_precision"))
        if i.get("close_only"):
            raise SystemExit(f"{self.cfg.symbol} в режиме close_only — новые позиции открывать нельзя")
        acc = self.c.account()
        resumed = False if self.cfg.dry_run else self.load_state()
        if self.cfg.balance_pct is not None and not resumed:
            self.base_size = self.size_from_balance(Decimal(acc["equity"]))
        self.refresh_instrument(force=True)
        log.info("Аккаунт: equity=%s uPnL=%s IM=%s MM=%s", acc.get("equity"), acc.get("unrealized_pnl"),
                 acc.get("initial_margin"), acc.get("maintenance_margin"))
        pos = self.c.position(self.cfg.symbol)
        log.info("Позиция %s: size=%s entry=%s liq=%s", self.cfg.symbol, pos.get("size"),
                 pos.get("entry_price"), pos.get("liquidation_price"))
        price = self.c.mark_price(self.cfg.symbol)
        log.info("Mark price: %s | сетка: %s | size=%s", price,
                 ", ".join(fmt(self.price_for(Slot(k, "buy"))) for k in range(len(self.levels))), self.size)
        if not (self.levels[0] < price < self.levels[-1]):
            log.warning("Цена %s вне диапазона сетки [%s, %s] — все ордера будут с одной стороны",
                        price, self.cfg.grid_lower, self.cfg.grid_upper)

        if self.cfg.dry_run:
            plan = initial_slots(self.levels, price)
            for s in sorted(plan, key=lambda s: s.level):
                log.info("[DRY_RUN] поставил бы %s %s @ %s", s.side.upper(), self.size, fmt(self.price_for(s)))
            return

        if resumed:
            log.info("Продолжаю по сохранённому состоянию: %d ордеров, %d в очереди, статистика %s",
                     len(self.orders), len(self.pending), self.stats)
            return
        canceled = self.c.cancel_all(self.cfg.symbol)
        if canceled:
            log.info("Отменено старых ордеров на %s: %d", self.cfg.symbol, len(canceled))
        self.orders = {}
        self.pending = [{"level": s.level, "side": s.side, "not_before": 0} for s in initial_slots(self.levels, price)]
        self.save_state()

    def shutdown(self):
        if self.cfg.dry_run or not self.cfg.cancel_on_exit:
            return
        try:
            canceled = self.c.cancel_all(self.cfg.symbol)
            log.info("Отменено ордеров при выходе: %d (позиция НЕ закрывается)", len(canceled))
            self.orders, self.pending = {}, []
            self.save_state()
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отменить ордера при выходе: %r", e)

    # ---------------- main tick ----------------
    def tick(self):
        if self.cfg.dry_run:
            log.info("[DRY_RUN] mark price %s = %s", self.cfg.symbol, self.c.mark_price(self.cfg.symbol))
            return
        self.refresh_instrument()
        open_ids = {o["order_id"] for o in self.c.open_orders(self.cfg.symbol)}
        for oid in [o for o in self.orders if o not in open_ids]:
            self._handle_closed(oid)
        self._place_pending()
        self.save_state()

    def _handle_closed(self, oid: str):
        info = self.orders[oid]
        o = self.c.order(oid)
        status = o.get("status")
        slot = Slot(info["level"], info["side"])
        if status in ("open", "queued", "untriggered"):
            return  # ещё жив (гонка между списком и деталями)
        del self.orders[oid]
        if status == "filled":
            fill = Decimal(o.get("fill_price") or info["price"])
            self.stats["buys" if slot.side == "buy" else "sells"] += 1
            if slot.side == "sell":
                self.stats["round_trips"] += 1
                profit = (Decimal(self.stats["grid_profit"]) +
                          (self.levels[slot.level] - self.levels[slot.level - 1]) * Decimal(o.get("size") or self.size))
                self.stats["grid_profit"] = str(profit.quantize(Decimal("0.0001")))
            log.info("ИСПОЛНЕН %s L%d %s @ %s | %s", slot.side.upper(), slot.level, o.get("size"), fill, self.stats)
            nxt = counter_slot(slot, len(self.levels))
            if nxt:
                self.pending.append({"level": nxt.level, "side": nxt.side, "not_before": 0})
        else:
            log.warning("Ордер %s L%d %s: %s (%s) — переставлю через %ds", oid, slot.level, slot.side,
                        status, o.get("reason"), REJECT_RETRY_SEC)
            self.pending.append({"level": slot.level, "side": slot.side, "not_before": time.time() + REJECT_RETRY_SEC})

    def _place_pending(self):
        now = time.time()
        keep = []
        busy = {(v["level"]) for v in self.orders.values()}
        for p in self.pending:
            if p["not_before"] > now:
                keep.append(p)
                continue
            if p["level"] in busy:
                log.debug("Уровень L%d уже занят — пропускаю %s", p["level"], p["side"])
                continue
            slot = Slot(p["level"], p["side"])
            try:
                order = self._place(slot)
            except ApiError as e:
                log.warning("Не удалось поставить %s L%d: %s", slot.side, slot.level, e)
                p["not_before"] = now + REJECT_RETRY_SEC
                keep.append(p)
                if e.code in ("rate_limited", "banned"):
                    keep.extend(x for x in self.pending if x is not p and x not in keep)
                    break
                continue
            busy.add(slot.level)
            if order.get("status") in ("rejected", "canceled"):
                log.warning("Ордер %s L%d сразу %s: %s", slot.side, slot.level, order["status"], order.get("reason"))
                p["not_before"] = now + REJECT_RETRY_SEC
                keep.append(p)
                busy.discard(slot.level)
                continue
            self.orders[order["order_id"]] = {"level": slot.level, "side": slot.side,
                                              "cid": order.get("client_order_id"), "price": order.get("price")}
            self.save_state()  # после каждого размещения — чтобы рестарт не потерял ордер
        self.pending = keep

    def _place(self, slot: Slot) -> dict:
        self.seq += 1
        cid = f"{self.cfg.bot_id}-{int(time.time() * 1000)}-{self.seq % 1000}"
        body = {
            "type": "limit",
            "symbol": self.cfg.symbol,
            "side": slot.side,
            "size": fmt(self.size),
            "price": fmt(self.price_for(slot)),
            "trigger_by": self.cfg.trigger_by,
            "max_slippage": fmt(self.cfg.max_slippage) if self.cfg.max_slippage is not None else None,
            "client_order_id": cid,
        }
        order = self.c.place_order(body)
        log.info("Поставлен %s L%d %s @ %s (id=%s, cid=%s, status=%s)", slot.side.upper(), slot.level,
                 body["size"], body["price"], order.get("order_id"), cid, order.get("status"))
        return order
