"""Движок бота: старт/восстановление, торговый цикл, риск-контроль, корректная остановка."""
import asyncio
import logging
import time
from decimal import Decimal

from ..config import Config
from ..exchange import AUTH, ApiError, OmniClient
from ..strategy.grid import Slot, initial_slots, make_levels
from ..strategy.precision import fmt
from .orders import FatalError, OrderManager
from .state import BotState, PendingSlot, StateStore

log = logging.getLogger("engine")

MAX_CONSECUTIVE_ERRORS = 30
STATUS_LOG_SEC = 300


class AuthFailure(FatalError):
    pass


class GridEngine:
    def __init__(self, cfg: Config, client: OmniClient, store: StateStore):
        self.cfg = cfg
        self.c = client
        self.store = store
        self.levels = make_levels(cfg.grid_lower, cfg.grid_upper, cfg.grid_levels)
        self.st = BotState()
        self.om: OrderManager | None = None
        self.lock = asyncio.Lock()
        self.last_beat = time.monotonic()
        self._last_status = 0.0
        self._errors = 0

    # ---------------------------------------------------------------- helpers
    def params(self) -> dict:
        sizing = f"pct:{self.cfg.balance_pct}" if self.cfg.balance_pct is not None else f"size:{self.cfg.order_size}"
        return {"symbol": self.cfg.symbol, "levels": [str(x) for x in self.levels], "sizing": sizing}

    def save(self):
        if not self.cfg.dry_run:
            self.store.save(self.st)

    def beat(self):
        self.last_beat = time.monotonic()

    def halt(self, reason: str):
        self.st.mode = "halted"
        self.st.halt_reason = reason
        self.save()
        log.critical("БОТ ОСТАНОВЛЕН: %s. Ордера сняты. Продолжить: make resume (или docker compose run --rm "
                     "gridbot resume) и перезапуск.", reason)

    # ---------------------------------------------------------------- startup
    async def start(self):
        try:
            st = await self.c.status()
        except ApiError as e:
            if e.code in AUTH:
                raise AuthFailure(str(e)) from e
            raise
        log.info("API: %s | ключ %s | %s", st.get("status"), self.c.signer.public_key, self.cfg.base_url)

        saved = self.store.load()
        resumed = bool(saved and saved.params == self.params())
        if saved and not resumed:
            log.warning("Параметры сетки изменились — строю сетку заново (старые ордера на %s будут отменены)",
                        self.cfg.symbol)
        self.st = saved if resumed else BotState(params=self.params())
        self.om = OrderManager(self.cfg, self.c, self.st, self.levels, self.save)

        acc = await self.c.account()
        log.info("Аккаунт: equity=%s, доступно=%s, uPnL=%s", acc.get("equity"),
                 (acc.get("cross") or {}).get("available"), acc.get("unrealized_pnl"))
        if not self.st.base_size:
            self.st.base_size = str(self.cfg.order_size if self.cfg.order_size is not None
                                    else self._size_from_balance(Decimal(acc["equity"])))
        await self.om.refresh_instrument(force=True)
        inst = self.om.inst
        log.info("%s: status=%s close_only=%s min_notional=%s size_step=%s", inst["symbol"], inst.get("status"),
                 inst.get("close_only"), inst.get("min_notional"), inst.get("size_step"))

        price = await self.c.mark_price(self.cfg.symbol)
        pos = await self.c.position(self.cfg.symbol)
        log.info("Цена %s | позиция %s (entry %s, liq %s, плечо %s)", price, pos.get("size"), pos.get("entry_price"),
                 pos.get("liquidation_price"), pos.get("leverage"))
        log.info("Сетка %s: %s | ордер %s | ~%s USDC на ордер", self.cfg.symbol,
                 " ".join(fmt(self.om.price_for(Slot(i, "buy")))
                          for i in range(len(self.levels))), fmt(self.om.size), (self.om.size * price).quantize(Decimal("0.01")))

        if self.cfg.dry_run:
            for s in sorted(initial_slots(self.levels, price), key=lambda s: s.level):
                log.info("[DRY_RUN] поставил бы %s %s @ %s", s.side.upper(), fmt(self.om.size), fmt(self.om.price_for(s)))
            return

        if self.st.mode == "halted":
            log.critical("Бот в режиме СТОП с прошлого запуска: %s. Ничего не делаю. "
                         "Продолжить: make resume (docker compose run --rm gridbot resume).", self.st.halt_reason)
            return
        if resumed and (self.st.orders or self.st.pending or self.st.mode == "paused"):
            log.info("Восстановлено: режим=%s, ордеров %d, в очереди %d, статистика %s", self.st.mode,
                     len(self.st.orders), len(self.st.pending), self.st.stats)
            return
        if Decimal(pos.get("size") or 0) != 0:
            log.warning("На %s уже есть позиция %s — бот её не трогает, сетка строится поверх",
                        self.cfg.symbol, pos.get("size"))
        await self.rebuild(price)

    def _size_from_balance(self, equity: Decimal) -> Decimal:
        budget = equity * self.cfg.balance_pct / 100
        per_order = budget / (len(self.levels) - 1)
        size = per_order / ((self.levels[0] + self.levels[-1]) / 2)
        log.info("Размер из баланса: %s × %s%% = %s USDC на сетку → %s USDC на ордер", equity, self.cfg.balance_pct,
                 budget.quantize(Decimal("0.01")), per_order.quantize(Decimal("0.01")))
        return size

    def _out_of_range(self, price: Decimal) -> bool:
        k = self.cfg.range_exit_pct / 100
        return price < self.cfg.grid_lower * (1 - k) or price > self.cfg.grid_upper * (1 + k)

    async def rebuild(self, price: Decimal):
        """Снять всё на символе и расставить сетку от текущей цены."""
        await self.om.cancel_all()
        if self.cfg.range_exit_action != "none" and self._out_of_range(price):
            self.st.mode = "paused"
            self.save()
            log.warning("Цена %s вне диапазона [%s, %s] — жду возврата в сетку", price, self.cfg.grid_lower,
                        self.cfg.grid_upper)
            return
        self.st.pending = [PendingSlot(s.level, s.side) for s in initial_slots(self.levels, price)]
        self.st.mode = "running"
        self.save()
        log.info("Сетка построена от цены %s: %d ордеров в очереди на размещение", price, len(self.st.pending))

    # ---------------------------------------------------------------- loops
    async def run(self, stop: asyncio.Event):
        await self.start()
        tasks = [asyncio.create_task(self._loop("trade", self._trade_step, self.cfg.poll_interval, stop)),
                 asyncio.create_task(self._loop("risk", self._risk_step, self.cfg.risk_interval, stop))]
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(tasks + [stopper], return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for t in done:
            if t is not stopper and t.exception():
                raise t.exception()

    async def _loop(self, name: str, step, interval: float, stop: asyncio.Event):
        while not stop.is_set():
            try:
                async with self.lock:
                    await step()
                self._errors = 0
            except (FatalError, asyncio.CancelledError):
                raise
            except ApiError as e:
                if e.code in AUTH:
                    raise AuthFailure(str(e)) from e
                self._on_error(name, e)
            except Exception as e:  # noqa: BLE001 — бот должен переживать сбои сети/биржи
                self._on_error(name, e)
            delay = interval * (1 + min(self._errors, 5))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _on_error(self, name: str, e: Exception):
        self._errors += 1
        log.error("[%s] ошибка (%d подряд): %r", name, self._errors, e)
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            raise FatalError(f"{self._errors} ошибок подряд — перезапуск контейнера")

    async def _trade_step(self):
        self.beat()
        if self.cfg.dry_run:
            log.info("[DRY_RUN] %s mark=%s", self.cfg.symbol, await self.c.mark_price(self.cfg.symbol))
            return
        if self.st.mode != "running":
            return
        await self.om.refresh_instrument()
        await self.om.reconcile()
        await self.om.place_pending()
        if not self.st.orders and not self.st.pending:
            log.warning("Сетка пуста — перестраиваю")
            await self.rebuild(await self.c.mark_price(self.cfg.symbol))
        self.save()

    async def _risk_step(self):
        self.beat()
        if self.cfg.dry_run or self.st.mode == "halted":
            return
        price = await self.c.mark_price(self.cfg.symbol)
        pos = await self.c.position(self.cfg.symbol)
        size = Decimal(pos.get("size") or 0)
        pnl = Decimal(pos.get("unrealized_pnl") or 0) + Decimal(pos.get("realized_pnl") or 0)

        # 1) аварийно большая позиция (ручные сделки / баг / пропущенные исполнения)
        cap = self.cfg.max_position_mult * (len(self.levels) - 1) * self.om.size
        if self.om.size and abs(size) > cap:
            await self.om.cancel_all()
            await self.om.flatten()
            return self.halt(f"позиция {size} больше лимита {cap}")

        # 2) стоп по убытку
        if self.cfg.max_loss_usdc is not None and pnl <= -self.cfg.max_loss_usdc:
            await self.om.cancel_all()
            await self.om.flatten()
            return self.halt(f"PnL позиции {pnl} <= -{self.cfg.max_loss_usdc} USDC")

        # 3) выход цены из диапазона
        if self.st.mode == "running" and self.cfg.range_exit_action != "none" and self._out_of_range(price):
            log.warning("Цена %s вышла за сетку [%s, %s] на >%s%%", price, self.cfg.grid_lower,
                        self.cfg.grid_upper, self.cfg.range_exit_pct)
            await self.om.cancel_all()
            if self.cfg.range_exit_action == "close":
                await self.om.flatten()
                return self.halt(f"цена {price} вне диапазона")
            self.st.mode = "paused"
            self.save()
            log.warning("Пауза: ордера сняты, позиция %s оставлена. Жду возврата цены в [%s, %s]",
                        size, self.cfg.grid_lower, self.cfg.grid_upper)
        elif self.st.mode == "paused" and self.cfg.grid_lower <= price <= self.cfg.grid_upper:
            log.info("Цена %s вернулась в диапазон — перестраиваю сетку", price)
            await self.rebuild(price)

        if time.monotonic() - self._last_status > STATUS_LOG_SEC:
            self._last_status = time.monotonic()
            log.info("СТАТУС: режим=%s цена=%s позиция=%s PnL=%s ордеров=%d очередь=%d %s | API weight %.0f/%d",
                     self.st.mode, price, size, pnl.quantize(Decimal("0.01")), len(self.st.orders),
                     len(self.st.pending), self.st.stats, self.c.limiter.used(), self.c.limiter.limit)

    # ---------------------------------------------------------------- shutdown
    async def shutdown(self):
        if self.cfg.dry_run or self.om is None or self.st.mode == "halted":
            return
        try:
            async with self.lock:
                if self.cfg.close_on_exit:
                    await self.om.cancel_all()
                    await self.om.flatten()
                elif self.cfg.cancel_on_exit:
                    await self.om.cancel_all()
                else:
                    log.info("Остановка: ордера остаются на бирже, состояние сохранено — после рестарта продолжу")
                self.save()
        except Exception as e:  # noqa: BLE001
            log.error("Ошибка при остановке: %r", e)
