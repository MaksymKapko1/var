import asyncio
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gridbot.exchange import ApiError, OmniClient, Signer, WeightLimiter

SEED = "0x" + "33" * 32
BASE = "https://api.omni.testnet.variational.io"


def make(handler, **kw):
    s = Signer(SEED, "testnet")
    return OmniClient(BASE, s, WeightLimiter(reserve=0), transport=httpx.MockTransport(handler), **kw)


def verify(req: httpx.Request):
    h = req.headers
    path = req.url.raw_path.decode()
    msg = "\n".join(["variational-omni-api/v1", "testnet", h["x-variational-key"], h["x-request-timestamp-ms"],
                     req.method, path, hashlib.sha256(req.content).hexdigest()])
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(h["x-variational-key"][2:])).verify(
        bytes.fromhex(h["x-variational-signature"][2:]), msg.encode())


def test_every_request_signed_over_exact_bytes():
    seen = []

    def h(req):
        verify(req)
        seen.append(req)
        if req.method == "POST":
            assert req.headers["content-type"] == "application/json"
            return httpx.Response(202, json={"placed": [{"order_id": "1", **json.loads(req.content)}]},
                                  headers={"X-RateLimit-Remaining": "55", "X-RateLimit-Limit": "60"})
        return httpx.Response(200, json={"results": []})

    async def go():
        c = make(h)
        o = await c.place_order({"type": "limit", "symbol": "ETH-PERP", "price": "1600.5", "client_order_id": "g-1-1"})
        assert o["order_id"] == "1"
        await c.open_orders("ETH-PERP")
        assert c.limiter.used() >= 5
        await c.aclose()
    asyncio.run(go())
    assert seen[1].url.raw_path.decode() == "/v1/orders?symbol=ETH-PERP"


def test_retries_rate_limited_and_overloaded(monkeypatch):
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error_code": "rate_limited", "retry_after_ms": 10})
        if calls["n"] == 2:
            return httpx.Response(503, json={"error_code": "overloaded", "retry_after_ms": 10})
        return httpx.Response(200, json={"status": "ok"})

    async def go():
        c = make(h)
        assert await c.status() == {"status": "ok"}
        await c.aclose()
    asyncio.run(go())
    assert calls["n"] == 3


def test_clock_skew_is_corrected():
    import time
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"error_code": "timestamp_out_of_window",
                                             "server_time_ms": int(time.time() * 1000) + 60000})
        assert int(req.headers["x-request-timestamp-ms"]) > time.time() * 1000 + 50000
        return httpx.Response(200, json={"status": "ok"})

    async def go():
        c = make(h)
        await c.status()
        await c.aclose()
    asyncio.run(go())


def test_auth_error_not_retried():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(403, json={"error_code": "insufficient_scope", "error_message": "need trade"})

    async def go():
        c = make(h)
        with pytest.raises(ApiError) as e:
            await c.cancel_all("ETH-PERP")
        assert e.value.code == "insufficient_scope"
        await c.aclose()
    asyncio.run(go())
    assert calls["n"] == 1


def test_duplicate_client_order_id_returns_existing():
    def h(req):
        return httpx.Response(409, json={"error_code": "duplicate_client_order_id", "order": {"order_id": "77"}})

    async def go():
        c = make(h)
        assert (await c.place_order({"client_order_id": "x"}))["order_id"] == "77"
        await c.aclose()
    asyncio.run(go())


def test_network_error_on_place_looks_up_by_cid():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if req.method == "POST":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"results": [{"order_id": "5", "status": "open"}]})

    async def go():
        c = make(h, max_retries=1)
        import gridbot.exchange.client as cl
        async def nosleep(*a): pass
        cl.asyncio.sleep, orig = nosleep, cl.asyncio.sleep
        try:
            assert (await c.place_order({"client_order_id": "abc"}))["order_id"] == "5"
        finally:
            cl.asyncio.sleep = orig
        await c.aclose()
    asyncio.run(go())
