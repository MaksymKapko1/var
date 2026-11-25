"""Сценарии движка против фейковой биржи (без сети)."""
import asyncio
import time
from decimal import Decimal as D

from gridbot.config import Config
from gridbot.core.engine import GridEngine
from gridbot.core.state import StateStore
from gridbot.exchange import ApiError, WeightLimiter


def cfg(tmp_path, **kw):
    base = dict(api_key="", api_secret="00" * 32, base_url="x", environment="testnet", symbol="ETH-PERP",
                grid_lower=D(1000), grid_upper=D(2000), grid_step_pct=None, grid_levels=11, order_size=D("0.0105"), balance_pct=None,
                trigger_by="quote", max_slippage=D("0.02"), range_exit_pct=D(3), range_exit_action="pause", recenter_flatten=False, recenter_min_sec=0,
                max_loss_usdc=None, max_position_mult=D("1.5"), close_max_slippage=D("0.05"),
                cancel_on_exit=False, close_on_exit=False, bot_id="t", poll_interval=3, risk_interval=3,
                rate_limit=60, rate_reserve=8, watchdog_sec=300, dry_run=False, data_dir=str(tmp_path),
                log_level="INFO")
    base.update(kw)
    c = Config(**base)
    c.validate()
    return c


class FakeSigner:
    public_key = "0xfake"


class FakeExchange:
    """Минимальная модель Omni: лимитки исполняются целиком при пересечении цены."""

    def __init__(self, price="1550", equity="1000"):
        self.price = D(price)
        self.equity = equity
        self.book: dict[str, dict] = {}
        self.n = 0
        self.pos = D(0)
        self.pnl = D(0)
        self.reject_next: list[str] = []      # причины отказа при исполнении
        self.http_fail_next: list[str] = []   # коды HTTP-ошибок при размещении
        self.signer = FakeSigner()
        self.limiter = WeightLimiter()
        self.cids = set()

    async def status(self): return {"status": "ok"}
    async def account(self): return {"equity": self.equity, "cross": {"available": self.equity}}
    async def instrument(self, s):
        return {"symbol": s, "status": "open", "close_only": False, "min_notional": "1", "size_step": "0.01",
                "price_precision": {"min_decimal_figures": 2, "max_decimal_only_figures": 4,
                                    "max_significant_figures": 6}}
    async def mark_price(self, s): return self.price
    async def position(self, s):
        return {"size": str(self.pos), "unrealized_pnl": str(self.pnl), "realized_pnl": "0"}
    async def open_orders(self, s): return [o for o in self.book.values() if o["status"] in ("open", "queued")]
    async def order(self, oid):
        if oid not in self.book:
            raise ApiError(404, "order_not_found")
        return self.book[oid]
    async def cancel_all(self, s):
        out = []
        for o in self.book.values():
            if o["status"] == "open":
                o["status"], o["reason"] = "canceled", "user_canceled"
                out.append(o)
        return out
    async def place_order(self, body, critical=False):
        assert body["client_order_id"] not in self.cids, "client_order_id reused"
        self.cids.add(body["client_order_id"])
        if self.http_fail_next:
            raise ApiError(400, self.http_fail_next.pop(0))
        self.n += 1
        o = dict(body, order_id=f"o{self.n}", status="open")
        if body["type"] == "market":
            o["status"], o["fill_price"] = "filled", str(self.price)
            self.pos += D(body["size"]) * (1 if body["side"] == "buy" else -1)
        self.book[o["order_id"]] = o
        return o

    def move(self, price):
        self.price = D(price)
        for o in self.book.values():
            if o["status"] != "open" or o["type"] != "limit":
                continue
            p = D(o["price"])
            if (o["side"] == "buy" and self.price <= p) or (o["side"] == "sell" and self.price >= p):
                if self.reject_next:
                    o["status"], o["reason"] = "rejected", self.reject_next.pop(0)
                    continue
                o["status"], o["fill_price"] = "filled", o["price"]
                self.pos += D(o["size"]) * (1 if o["side"] == "buy" else -1)

    def live(self):
        return {(int(D(o["price"])), o["side"]) for o in self.book.values() if o["status"] == "open"}


def engine(tmp_path, ex, **kw):
    c = cfg(tmp_path, **kw)
    return GridEngine(c, ex, StateStore(c.state_file))


async def step(e):
    await e._trade_step()
    await e._risk_step()


def test_build_fill_and_counter_orders(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start()
        await step(e)
        assert e.om.size == D("0.01")
        assert len(ex.live()) == 10
        assert all(s == "buy" for p, s in ex.live() if p < 1550) and all(s == "sell" for p, s in ex.live() if p > 1550)
        ex.move("1390")              # BUY 1400 исполнился (открытие, не цикл)
        await step(e)
        assert (1500, "sell") in ex.live() and e.st.stats["round_trips"] == 0
        ex.move("1505")              # SELL 1500 исполнился
        await step(e)
        assert (1400, "buy") in ex.live()
        assert e.st.stats["round_trips"] == 1 and D(e.st.stats["grid_profit"]) == D("1.0")
        assert len(ex.live()) == 10
    asyncio.run(go())


def test_exchange_reject_backoff_and_retry(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        ex.reject_next = ["quote_unavailable"]
        ex.move("1395")                       # BUY 1400 отклонён при исполнении
        await step(e)
        p = [p for p in e.st.pending if p.level == 4][0]
        assert p.attempts == 1 and p.last_error == "rejected:quote_unavailable" and p.not_before > time.time()
        p.not_before = 0                      # «прошло время»
        await step(e)
        assert (1400, "buy") in ex.live()     # переставлен после паузы
        ex.move("1394")
        await step(e)
        assert (1500, "sell") in ex.live()    # исполнен -> встречный ордер
    asyncio.run(go())


def test_http_place_error_backoff(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        ex.http_fail_next = ["insufficient_margin_fake", "too_many_orders"]
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        assert len(ex.live()) == 8 and len(e.st.pending) == 2
        assert all(p.not_before > time.time() for p in e.st.pending)
    asyncio.run(go())


def test_restart_resumes_without_duplicates(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        n = ex.n
        e2 = engine(tmp_path, ex)
        await e2.start(); await step(e2)
        assert ex.n == n and len(ex.live()) == 10
        ex.move("1390")                       # исполнилось, пока «контейнер лежал»
        e3 = engine(tmp_path, ex)
        await e3.start(); await step(e3)
        assert (1500, "sell") in ex.live()
    asyncio.run(go())


def test_param_change_rebuilds(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        e2 = engine(tmp_path, ex, grid_levels=6)
        await e2.start(); await step(e2)
        assert len(ex.live()) == 5
    asyncio.run(go())


def test_range_exit_pause_and_resume(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        ex.move("2100")                       # > 2000*1.03 = 2060
        await step(e)
        assert e.st.mode == "paused" and not ex.live()
        ex.move("1800")
        await step(e); await step(e)
        assert e.st.mode == "running" and len(ex.live()) == 10
    asyncio.run(go())


def test_range_exit_close_halts_and_flattens(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex, range_exit_action="close")
        await e.start(); await step(e)
        ex.move("1390"); await step(e)
        assert ex.pos > 0
        ex.move("900")                        # < 1000*0.97
        await step(e)
        assert e.st.mode == "halted" and ex.pos == 0 and not ex.live()
        e2 = engine(tmp_path, ex, range_exit_action="close")   # рестарт не возобновляет торговлю
        await e2.start(); await step(e2)
        assert not ex.live()
    asyncio.run(go())


def test_max_loss_stop(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex, max_loss_usdc=D(50))
        await e.start(); await step(e)
        ex.move("1390"); await step(e)
        ex.pnl = D(-60)
        await step(e)
        assert e.st.mode == "halted" and ex.pos == 0
    asyncio.run(go())


def test_balance_pct_sizing(tmp_path):
    async def go():
        ex = FakeExchange("1550", equity="1000")
        # 1000 × 15% = 150 / 10 ордеров = 15 USDC / mid 1500 = 0.01
        e = engine(tmp_path, ex, order_size=None, balance_pct=D(15))
        await e.start(); await step(e)
        assert e.om.size == D("0.01")
        ex.equity = "5000"
        e2 = engine(tmp_path, ex, order_size=None, balance_pct=D(15))
        await e2.start()
        assert e2.om.size == D("0.01")       # не пересчитывается на рестарте
    asyncio.run(go())


def test_dry_run_places_nothing(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex, dry_run=True)
        await e.start(); await step(e); await e.shutdown()
        assert ex.n == 0
    asyncio.run(go())


def test_shutdown_close_on_exit(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex, close_on_exit=True)
        await e.start(); await step(e)
        ex.move("1390"); await step(e)
        await e.shutdown()
        assert not ex.live() and ex.pos == 0
    asyncio.run(go())


def test_run_loop_stops_on_signal(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        stop = asyncio.Event()
        task = asyncio.create_task(e.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert len(ex.live()) == 10
    asyncio.run(go())


def test_initial_sell_fill_is_not_a_round_trip(tmp_path):
    async def go():
        ex = FakeExchange("1550")
        e = engine(tmp_path, ex)
        await e.start(); await step(e)
        ex.move("1610")                       # начальный SELL 1600 (открытие шорта)
        await step(e)
        assert e.st.stats["round_trips"] == 0 and (1500, "buy") in ex.live()
        ex.move("1495")                       # встречный BUY 1500 -> цикл закрыт
        await step(e)
        assert e.st.stats["round_trips"] == 1 and D(e.st.stats["grid_profit"]) == D("1.0")
    asyncio.run(go())


# ------------------------------------------------------------------ авто-сетка
def auto(tmp_path, ex, **kw):
    base = dict(grid_lower=None, grid_upper=None, grid_step_pct=D("0.5"), grid_levels=6,
                range_exit_action="recenter", range_exit_pct=D(0))
    base.update(kw)
    return engine(tmp_path, ex, **base)


def test_auto_grid_centered_on_price(tmp_path):
    async def go():
        ex = FakeExchange("2000")
        e = auto(tmp_path, ex)
        await e.start(); await step(e)
        # 6 уровней, шаг 0.5%: 1975 1985 1995 2005 2015 2025
        assert [int(x) for x in e.levels] == [1975, 1985, 1995, 2005, 2015, 2025]
        assert len(ex.live()) == 5
        assert {s for p, s in ex.live() if p < 2000} == {"buy"} and {s for p, s in ex.live() if p > 2000} == {"sell"}
    asyncio.run(go())


def test_auto_recenter_follows_price(tmp_path):
    async def go():
        ex = FakeExchange("2000")
        e = auto(tmp_path, ex)
        await e.start(); await step(e)
        ex.move("2100")                          # всё SELL выше исполнилось, цена ушла вверх
        await step(e)
        assert e.st.stats["recenters"] == 1 and e.st.mode == "running"
        assert e.levels[0] < D(2100) < e.levels[-1]
        await step(e)
        live = ex.live()
        assert len(live) == 5 and all(abs(p - 2100) < 40 for p, _ in live)
    asyncio.run(go())


def test_auto_recenter_with_flatten(tmp_path):
    async def go():
        ex = FakeExchange("2000")
        e = auto(tmp_path, ex, recenter_flatten=True)
        await e.start(); await step(e)
        ex.move("1900")                          # набрали лонг на всех BUY
        assert ex.pos > 0
        await step(e)
        assert ex.pos == 0 and e.st.stats["recenters"] == 1
    asyncio.run(go())


def test_auto_recenter_cooldown(tmp_path):
    async def go():
        ex = FakeExchange("2000")
        e = auto(tmp_path, ex, recenter_min_sec=3600)
        await e.start(); await step(e)
        ex.move("2100"); await step(e)
        ex.move("2300"); await step(e)
        assert e.st.stats["recenters"] == 1      # второй раз не чаще раза в час
    asyncio.run(go())


def test_auto_restart_keeps_levels(tmp_path):
    async def go():
        ex = FakeExchange("2000")
        e = auto(tmp_path, ex)
        await e.start(); await step(e)
        lv, n = list(e.levels), ex.n
        ex.price = D("2003")
        e2 = auto(tmp_path, ex)
        await e2.start(); await step(e2)
        assert e2.levels == lv and ex.n == n
    asyncio.run(go())


def test_auto_too_small_step_is_fatal(tmp_path):
    import pytest
    from gridbot.core.orders import FatalError

    async def go():
        ex = FakeExchange("0.5")                 # цена 0.5, 4 значащих -> шаг 0.001% слипнется
        e = auto(tmp_path, ex, grid_step_pct=D("0.001"), order_size=D(100))
        with pytest.raises(FatalError):
            await e.start()
    asyncio.run(go())


def test_config_modes():
    import os
    import pytest
    from gridbot.config import Config, ConfigError
    env = {"VAR_API_SECRET": "11" * 32, "GRID_STEP_PCT": "0.2", "GRID_BALANCE_PCT": "10"}
    old = dict(os.environ)
    try:
        os.environ.update(env)
        c = Config.from_env()
        assert c.auto and c.range_exit_action == "recenter" and c.range_exit_pct == 0
        os.environ.pop("GRID_STEP_PCT")
        with pytest.raises(ConfigError):
            Config.from_env()                     # ни шага, ни диапазона
    finally:
        os.environ.clear(); os.environ.update(old)
