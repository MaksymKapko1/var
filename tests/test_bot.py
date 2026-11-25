from decimal import Decimal as D

from gridbot.bot import GridBot
from gridbot.config import Config
from gridbot.grid import Slot, counter_slot, initial_slots, make_levels


def cfg(tmp_path, **kw):
    base = dict(api_key="", api_secret="00" * 32, base_url="x", environment="testnet", symbol="ETH-PERP",
                grid_lower=D(1000), grid_upper=D(2000), grid_levels=11, order_size=D("0.0105"), balance_pct=None,
                trigger_by="quote", max_slippage=D("0.02"), bot_id="t", poll_interval=0, dry_run=False,
                cancel_on_exit=True, state_file=str(tmp_path / "state.json"), log_level="INFO")
    base.update(kw)
    return Config(**base)


class FakeApi:
    def __init__(self, price="1550"):
        self.price = D(price)
        self.book = {}  # order_id -> order
        self.n = 0
        self.placed = []

    def status(self): return {"status": "ok"}
    def instrument(self, s): return {"symbol": s, "status": "open", "close_only": False, "min_notional": "10",
                                     "size_step": "0.01", "price_precision": {"min_decimal_figures": 2,
                                     "max_decimal_only_figures": 4, "max_significant_figures": 6}}
    def account(self): return {"equity": "1000"}
    def position(self, s): return {"size": "0"}
    def mark_price(self, s): return self.price
    def cancel_all(self, s):
        out = [o for o in self.book.values() if o["status"] == "open"]
        for o in out: o["status"] = "canceled"
        return out
    def open_orders(self, s): return [o for o in self.book.values() if o["status"] in ("open", "queued")]
    def order(self, oid): return self.book[oid]
    def place_order(self, body):
        self.n += 1
        o = dict(body, order_id=f"o{self.n}", status="open")
        self.book[o["order_id"]] = o
        self.placed.append(body)
        return o

    def move(self, price):
        """симулируем исполнение лимиток при движении цены"""
        self.price = D(price)
        for o in self.book.values():
            if o["status"] != "open": continue
            p = D(o["price"])
            if (o["side"] == "buy" and self.price <= p) or (o["side"] == "sell" and self.price >= p):
                o["status"] = "filled"; o["fill_price"] = o["price"]


def by_level(bot):
    return {v["level"]: v["side"] for v in bot.orders.values()}


def test_grid_helpers():
    lv = make_levels(D(100), D(200), 11)
    slots = initial_slots(lv, D(152))
    assert len(slots) == 10 and Slot(5, "buy") not in slots and Slot(5, "sell") not in slots
    assert {s.side for s in slots if s.level < 5} == {"buy"} and {s.side for s in slots if s.level > 5} == {"sell"}
    assert counter_slot(Slot(3, "buy"), 11) == Slot(4, "sell")
    assert counter_slot(Slot(10, "buy"), 11) is None
    assert counter_slot(Slot(0, "sell"), 11) is None


def test_full_cycle(tmp_path):
    api = FakeApi("1550")
    bot = GridBot(cfg(tmp_path), api)
    bot.startup()
    assert bot.size == D("0.01")  # округлено под size_step
    bot.tick()
    assert len(bot.orders) == 10
    levels = by_level(bot)
    assert 5 not in levels  # пустой уровень рядом с ценой
    assert all(s == "buy" for l, s in levels.items() if l < 5)
    body = api.placed[0]
    assert body["type"] == "limit" and body["max_slippage"] == "0.02" and body["size"] == "0.01"

    api.move("1390")  # исполнились buy L4 (1400)
    bot.tick()
    assert by_level(bot)[5] == "sell" and 4 not in by_level(bot)
    api.move("1505")  # sell L5 (1500) исполнился
    bot.tick()
    assert by_level(bot)[4] == "buy"
    assert bot.stats["round_trips"] == 1 and D(bot.stats["grid_profit"]) == D("1.0")
    assert len(bot.orders) == 10


def test_restart_resumes_state(tmp_path):
    api = FakeApi("1550")
    bot = GridBot(cfg(tmp_path), api)
    bot.startup(); bot.tick()
    ids = set(bot.orders)
    bot2 = GridBot(cfg(tmp_path), api)
    bot2.startup(); bot2.tick()
    assert set(bot2.orders) == ids and api.n == 10  # ничего не переставлял


def test_rejected_is_retried_later(tmp_path):
    api = FakeApi("1550")
    bot = GridBot(cfg(tmp_path), api)
    bot.startup(); bot.tick()
    oid = next(iter(bot.orders))
    api.book[oid]["status"] = "rejected"; api.book[oid]["reason"] = "insufficient_margin"
    bot.tick()
    assert oid not in bot.orders and len(bot.pending) == 1 and bot.pending[0]["not_before"] > 0


def test_shutdown_cancels(tmp_path):
    api = FakeApi("1550")
    bot = GridBot(cfg(tmp_path), api)
    bot.startup(); bot.tick(); bot.shutdown()
    assert not api.open_orders("ETH-PERP") and bot.orders == {}


def test_dry_run_places_nothing(tmp_path):
    api = FakeApi("1550")
    bot = GridBot(cfg(tmp_path, dry_run=True), api)
    bot.startup(); bot.tick(); bot.shutdown()
    assert api.n == 0


def test_balance_pct_sizing_and_persist(tmp_path):
    api = FakeApi("1550")
    # equity 1000 × 15% = 150 USDC на сетку / 10 ордеров = 15 USDC / mid 1500 = 0.01
    c = cfg(tmp_path, order_size=None, balance_pct=D(15))
    bot = GridBot(c, api)
    bot.startup(); bot.tick()
    assert bot.size == D("0.01") and api.placed[0]["size"] == "0.01"
    api.account = lambda: {"equity": "5000"}  # баланс вырос — на рестарте размер не меняется
    bot2 = GridBot(c, api)
    bot2.startup()
    assert bot2.size == D("0.01")
