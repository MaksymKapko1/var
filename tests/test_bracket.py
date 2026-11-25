import asyncio
import time
from decimal import Decimal as D

import pytest

from gridbot.core.bracket import BracketEngine
from gridbot.exchange import ApiError, WeightLimiter
from test_engine import cfg as base_cfg  # noqa: E402

PREC = {"min_decimal_figures": 2, "max_decimal_only_figures": 4, "max_significant_figures": 6}


def cfg(tmp_path, **kw):
    b = dict(strategy="bracket", bracket_symbols=("US500-PERP", "XAU-PERP"), bracket_margin_usdc=D(100),
             bracket_notional_usdc=None, bracket_tp_pct=D("0.1"), bracket_sl_pct=D("0.2"),
             bracket_entry_offset_bps=D(1), bracket_entry_ttl_sec=90, bracket_max_hold_min=30,
             bracket_cooldown_sec=0, bracket_poll_sec=10, bracket_fast=3, bracket_slow=6, bracket_min_samples=3,
             bracket_session_guard_min=5)
    b.update(kw)
    return base_cfg(tmp_path, **b)


class FakeSigner:
    public_key = "0xfake"


class Ex:
    """Omni-подобная биржа: лимитный вход + прикреплённые TP/SL, торговые сессии."""

    def __init__(self):
        self.price = {"US500-PERP": D("6000"), "XAU-PERP": D("2650")}
        self.pos = {s: D(0) for s in self.price}
        self.book = {}
        self.n = 0
        self.status_ = {s: "open" for s in self.price}
        self.next_close = {s: None for s in self.price}
        self.signer, self.limiter = FakeSigner(), WeightLimiter()
        self.reject_entry = None

    async def status(self): return {"status": "ok"}
    async def account(self): return {"equity": "10000", "cross": {"available": "10000"}}
    async def instrument(self, s):
        return {"symbol": s, "status": self.status_[s], "close_only": False, "min_notional": "1", "size_step": "0.0001",
                "price_precision": PREC, "next_close_at": self.next_close[s], "next_open_at": None}
    async def mark_price(self, s): return self.price[s]
    async def position(self, s): return {"size": str(self.pos[s]), "leverage": "20", "mark_price": str(self.price[s])}
    async def order(self, oid): return self.book[oid]
    async def cancel_order(self, oid):
        o = self.book[oid]
        if o["status"] == "filled":
            raise ApiError(409, "order_filled")
        o["status"], o["reason"] = "canceled", "user_canceled"
        for k in self.book.values():
            if k.get("parent_order_id") == oid and k["status"] == "untriggered":
                k["status"], k["reason"] = "canceled", "parent_canceled"
        return o
    async def cancel_all(self, s):
        out = []
        for o in self.book.values():
            if o["symbol"] == s and o["status"] in ("open", "untriggered"):
                o["status"] = "canceled"; out.append(o)
        return out

    def _new(self, **o):
        self.n += 1
        o["order_id"] = f"o{self.n}"
        self.book[o["order_id"]] = o
        return o

    async def place_orders(self, body, critical=False):
        if self.reject_entry:
            raise ApiError(400, self.reject_entry)
        p = self._new(**{k: v for k, v in body.items() if k not in ("take_profit", "stop_loss")},
                      status="open" if body["type"] == "limit" else "filled", parent_order_id=None)
        out = [p]
        opp = "sell" if body["side"] == "buy" else "buy"
        for t in ("take_profit", "stop_loss"):
            if body.get(t):
                out.append(self._new(type=t, symbol=body["symbol"], side=opp, size=body["size"],
                                     trigger_price=body[t]["trigger_price"], status="untriggered",
                                     parent_order_id=p["order_id"]))
        if body["type"] == "market":
            self._fill(p, self.price[body["symbol"]])
        return out

    async def place_order(self, body, critical=False):
        return (await self.place_orders(body, critical))[0]

    def _fill(self, o, px):
        o["status"], o["fill_price"] = "filled", str(px)
        self.pos[o["symbol"]] += D(o["size"]) * (1 if o["side"] == "buy" else -1)

    def move(self, s, px):
        self.price[s] = D(px)
        for o in list(self.book.values()):
            if o["symbol"] != s:
                continue
            if o["type"] == "limit" and o["status"] == "open":
                if (o["side"] == "buy" and self.price[s] <= D(o["price"])) or (o["side"] == "sell" and self.price[s] >= D(o["price"])):
                    self._fill(o, D(o["price"]))
        for o in list(self.book.values()):
            if o["symbol"] != s or o["status"] != "untriggered":
                continue
            par = self.book[o["parent_order_id"]]
            if par["status"] != "filled" or self.pos[s] == 0:
                continue
            tp = D(o["trigger_price"])
            long_pos = par["side"] == "buy"
            hit = ((o["type"] == "take_profit" and (self.price[s] >= tp if long_pos else self.price[s] <= tp)) or
                   (o["type"] == "stop_loss" and (self.price[s] <= tp if long_pos else self.price[s] >= tp)))
            if hit:
                self._fill(o, self.price[s])
                for k in self.book.values():
                    if k.get("parent_order_id") == o["parent_order_id"] and k["status"] == "untriggered":
                        k["status"], k["reason"] = "canceled", "no_position_to_reduce"


async def warm(e, ex, s, prices):
    for p in prices:
        ex.price[s] = D(p)
        await e.process(s)


def phase(e, s):
    return e.st.sym(s)["phase"]


def test_trend_long_entry_with_tp_sl_and_take_profit(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])   # растёт -> long
        ss = e.st.sym("US500-PERP")
        assert ss["phase"] == "entry" and ss["side"] == "long"
        assert D(ss["entry_px"]) == D("5999.4")                       # 1 б.п. ниже mark
        assert D(ss["tp_px"]) > D(ss["entry_px"]) > D(ss["sl_px"])
        # 100$ × 20x = 2000$ / 6000
        assert D(ss["size"]) == D("0.3333")
        ex.move("US500-PERP", "5999")                                 # вход исполнился
        await e.process("US500-PERP")
        assert phase(e, "US500-PERP") == "position"
        ex.move("US500-PERP", "6006")                                 # TP +0.1%
        await e.process("US500-PERP")
        assert phase(e, "US500-PERP") == "flat" and e.st.stats["tp"] == 1 and ex.pos["US500-PERP"] == 0
        assert D(e.st.stats["pnl_est_usdc"]) > 0
    asyncio.run(go())


def test_downtrend_short_and_stop_loss(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "XAU-PERP", ["2660", "2655", "2650"])
        assert e.st.sym("XAU-PERP")["side"] == "short"
        ex.move("XAU-PERP", "2651"); await e.process("XAU-PERP")
        assert phase(e, "XAU-PERP") == "position" and ex.pos["XAU-PERP"] < 0
        ex.move("XAU-PERP", "2657"); await e.process("XAU-PERP")      # SL +0.2%
        assert e.st.stats["sl"] == 1 and ex.pos["XAU-PERP"] == 0 and phase(e, "XAU-PERP") == "flat"
        assert not [o for o in ex.book.values() if o["status"] in ("open", "untriggered")]
    asyncio.run(go())


def test_unfilled_entry_is_repriced_after_ttl(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])
        old = e.st.sym("US500-PERP")["entry_id"]
        e.st.sym("US500-PERP")["placed_at"] -= 100                    # прошло 100с > TTL
        ex.price["US500-PERP"] = D("6010")
        await e.process("US500-PERP")                                 # отмена
        assert ex.book[old]["status"] == "canceled" and e.st.stats["reprices"] == 1
        await e.process("US500-PERP")                                 # новый вход по новой цене
        ss = e.st.sym("US500-PERP")
        assert ss["entry_id"] != old and D(ss["entry_px"]) > D("6000")
        assert all(o["status"] == "canceled" for o in ex.book.values() if o.get("parent_order_id") == old)
    asyncio.run(go())


def test_max_hold_closes_by_market(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])
        ex.move("US500-PERP", "5999"); await e.process("US500-PERP")
        e.st.sym("US500-PERP")["opened_at"] -= 31 * 60
        await e.process("US500-PERP")
        assert ex.pos["US500-PERP"] == 0 and e.st.stats["timeouts"] == 1 and phase(e, "US500-PERP") == "flat"
    asyncio.run(go())


def test_market_closed_and_session_guard(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        ex.status_["US500-PERP"] = "closed"
        e._inst.clear()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000", "6005"])
        assert phase(e, "US500-PERP") == "flat" and ex.n == 0           # закрыт — не входим
        ex.status_["US500-PERP"] = "open"
        e._inst.clear()
        await e.process("US500-PERP")
        ex.move("US500-PERP", "6004"); await e.process("US500-PERP")
        assert phase(e, "US500-PERP") == "position"
        ex.next_close["US500-PERP"] = int((time.time() + 120) * 1000)  # закрытие через 2 мин
        e._inst.clear()
        await e.process("US500-PERP")
        assert ex.pos["US500-PERP"] == 0 and e.st.stats["session_closes"] == 1
        n = ex.n
        await e.process("US500-PERP")
        assert ex.n == n                                                 # перед закрытием не входим
    asyncio.run(go())


def test_place_error_backoff_and_symbol_isolation(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        ex.reject_entry = "market_limit_exceeded"
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])
        ss = e.st.sym("US500-PERP")
        assert ss["phase"] == "flat" and ss["retry_at"] > time.time() + 200
        ex.reject_entry = None
        await warm(e, ex, "XAU-PERP", ["2640", "2645", "2650"])          # другой символ работает
        assert phase(e, "XAU-PERP") == "entry"
    asyncio.run(go())


def test_restart_resumes_position(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])
        ex.move("US500-PERP", "5999"); await e.process("US500-PERP")
        e.save()
        e2 = BracketEngine(cfg(tmp_path), ex)
        await e2.start()
        assert ex.pos["US500-PERP"] > 0 and phase(e2, "US500-PERP") == "position"
        ex.move("US500-PERP", "6006"); await e2.process("US500-PERP")
        assert e2.st.stats["tp"] == 1
    asyncio.run(go())


def test_unknown_position_flattened_on_clean_start(tmp_path):
    async def go():
        ex = Ex()
        ex.pos["XAU-PERP"] = D("1")
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        assert ex.pos["XAU-PERP"] == 0
    asyncio.run(go())


def test_close_all_and_dry_run(tmp_path):
    async def go():
        ex = Ex()
        e = BracketEngine(cfg(tmp_path), ex)
        await e.start()
        await warm(e, ex, "US500-PERP", ["5990", "5995", "6000"])
        ex.move("US500-PERP", "5999"); await e.process("US500-PERP")
        await e.close_all()
        assert ex.pos["US500-PERP"] == 0 and e.st.d["halted"]
        await e.process("US500-PERP")
        assert phase(e, "US500-PERP") == "flat"
        ex2 = Ex()
        e2 = BracketEngine(cfg(tmp_path / "d", dry_run=True), ex2)
        await e2.start(); await warm(e2, ex2, "US500-PERP", ["1", "2", "3", "4"])
        assert ex2.n == 0
    asyncio.run(go())
