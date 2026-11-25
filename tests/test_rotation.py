import asyncio
import time
from decimal import Decimal as D

from gridbot.config import Config, _legs
from gridbot.core.rotation import RotationEngine
from gridbot.exchange import ApiError, WeightLimiter
from test_engine import cfg as grid_cfg  # noqa: E402

PRICES = {"BTC-PERP": D("90000"), "ETH-PERP": D("1700"), "HYPE-PERP": D("30"), "SOL-PERP": D("130")}


def cfg(tmp_path, **kw):
    base = dict(strategy="rotation", rotation_legs=_legs("BTC:long,ETH:short,HYPE:long,SOL:short"),
                rotation_margin_usdc=D(100), rotation_interval_min=60, rotation_check_sec=60,
                rotation_max_slippage=D("0.01"))
    base.update(kw)
    c = grid_cfg(tmp_path, **base)
    return c


class FakeSigner:
    public_key = "0xfake"


class FakeEx:
    def __init__(self):
        self.pos = {s: D(0) for s in PRICES}
        self.book = {}
        self.n = 0
        self.reject = []          # причины отказа для следующих market-ордеров
        self.signer = FakeSigner()
        self.limiter = WeightLimiter()
        self.lev = "50"

    async def status(self): return {"status": "ok"}
    async def account(self): return {"equity": "10000", "cross": {"available": "10000"}}
    async def instrument(self, s):
        return {"symbol": s, "status": "open", "close_only": False, "min_notional": "1", "size_step": "0.00001"}
    async def mark_price(self, s): return PRICES[s]
    async def position(self, s):
        return {"symbol": s, "size": str(self.pos[s]), "leverage": self.lev, "mark_price": str(PRICES[s])}
    async def order(self, oid): return self.book[oid]
    async def cancel_all(self, s): return []
    async def place_order(self, body, critical=False):
        self.n += 1
        o = dict(body, order_id=f"o{self.n}")
        if self.reject and not body.get("reduce_only"):
            o.update(status="rejected", reason=self.reject.pop(0))
        else:
            o.update(status="filled", fill_price=str(PRICES[body["symbol"]]))
            sz = D(body["size"]) * (1 if body["side"] == "buy" else -1)
            self.pos[body["symbol"]] += sz
        self.book[o["order_id"]] = o
        return o


def run(coro):
    return asyncio.run(coro)


def test_opens_all_legs_with_margin_times_leverage(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        # 100$ × 50x = 5000$ на ногу
        assert ex.pos["BTC-PERP"] == D("0.05555")   # 5000/90000 вниз до size_step
        assert ex.pos["ETH-PERP"] < 0 and ex.pos["SOL-PERP"] < 0 and ex.pos["HYPE-PERP"] > 0
        assert abs(ex.pos["HYPE-PERP"] * 30 - 5000) < 1
        assert e.st.stats["opens"] == 4 and e.st.stats["rotations"] == 1
    run(go())


def test_no_reopen_before_interval_but_restores_missing_leg(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        n = ex.n
        await e.step()
        assert ex.n == n                      # всё открыто, ничего не делаем
        ex.pos["SOL-PERP"] = D(0)             # «ликвидировали»
        await e.step()
        assert ex.pos["SOL-PERP"] < 0 and ex.n == n + 1
    run(go())


def test_hourly_rotation_closes_and_reopens(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        before = dict(ex.pos)
        e.st.d["next_rotation"] = time.time() - 1   # «прошёл час»
        await e.step()
        assert e.st.stats["rotations"] == 2 and e.st.stats["closes"] == 4 and e.st.stats["opens"] == 8
        assert ex.pos == before
        assert e.st.d["next_rotation"] > time.time() + 3500
        assert D(e.st.stats["volume_usdc"]) > 55000
    run(go())


def test_wrong_side_is_flipped(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        ex.pos["BTC-PERP"] = D("-0.01")
        await e.step()
        assert ex.pos["BTC-PERP"] > 0
    run(go())


def test_reject_retries_then_backs_off(tmp_path, monkeypatch):
    import gridbot.core.rotation as r
    async def nosleep(*a): pass
    monkeypatch.setattr(r.asyncio, "sleep", nosleep)

    async def go():
        ex = FakeEx()
        ex.reject = ["quote_unavailable"]     # 1-я попытка BTC отклонена, 2-я проходит
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        assert ex.pos["BTC-PERP"] > 0 and e.st.stats["rejects"] == 1
        ex.pos["ETH-PERP"] = D(0)
        ex.reject = ["insufficient_margin"] * 3
        await e.step()
        assert ex.pos["ETH-PERP"] == 0 and e._retry_at["ETH-PERP"] > time.time()
        n = ex.n
        await e.step()                        # в бэкоффе — не долбим
        assert ex.n == n
    run(go())


def test_close_all_halts(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        await e.close_all()
        assert all(v == 0 for v in ex.pos.values())
        e2 = RotationEngine(cfg(tmp_path), ex)
        await e2.start(); await e2.step()
        assert all(v == 0 for v in ex.pos.values())   # после рестарта не открывает
    run(go())


def test_restart_keeps_schedule(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path), ex)
        await e.start(); await e.step()
        n = ex.n
        e2 = RotationEngine(cfg(tmp_path), ex)
        await e2.start(); await e2.step()
        assert ex.n == n                      # рестарт не вызывает внеплановую ротацию
    run(go())


def test_notional_override_and_dry_run(tmp_path):
    async def go():
        ex = FakeEx()
        e = RotationEngine(cfg(tmp_path, rotation_notional_usdc=D(200)), ex)
        await e.start(); await e.step()
        assert abs(ex.pos["SOL-PERP"] * 130 + 200) < 1
        ex2 = FakeEx()
        e2 = RotationEngine(cfg(tmp_path / "d", dry_run=True), ex2)
        await e2.start(); await e2.step()
        assert ex2.n == 0
    run(go())


def test_legs_parsing():
    import pytest
    from gridbot.config import ConfigError
    assert _legs("btc:long, ETH-PERP:sell") == (("BTC-PERP", "long"), ("ETH-PERP", "short"))
    with pytest.raises(ConfigError):
        _legs("BTC:up")
