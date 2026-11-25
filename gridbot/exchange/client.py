"""Асинхронный клиент Variational Omni API (httpx)."""
import asyncio
import json
import logging
import random
import time
from decimal import Decimal
from urllib.parse import quote, urlencode

import httpx

from .errors import AUTH, RETRYABLE, ApiError, NetworkError
from .ratelimit import WeightLimiter
from .signer import Signer

log = logging.getLogger("api")

# Оценка веса запросов (уточняется по заголовкам X-RateLimit-*).
WEIGHT = {"GET": 1, "POST": 2, "DELETE": 2}


class OmniClient:
    def __init__(self, base_url: str, signer: Signer, limiter: WeightLimiter,
                 timeout: float = 15, max_retries: int = 4, transport: httpx.AsyncBaseTransport | None = None):
        self.signer = signer
        self.limiter = limiter
        self.max_retries = max_retries
        self.clock_offset_ms = 0
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                       headers={"user-agent": "gridbot/2"})

    async def aclose(self):
        await self._http.aclose()

    # ------------------------------------------------------------------ core
    async def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                      critical: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path_q = path + ("?" + urlencode(params, quote_via=quote) if params else "")
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire(WEIGHT.get(method, 1), critical=critical)
            ts = int(time.time() * 1000) + self.clock_offset_ms
            headers = self.signer.headers(ts, method, path_q, data)
            if data:
                headers["content-type"] = "application/json"
            try:
                resp = await self._http.request(method, path_q, content=data or None, headers=headers)
            except httpx.HTTPError as e:
                last_exc = NetworkError(f"{method} {path}: {e!r}")
                await self._backoff(attempt, f"сеть: {e!r}")
                continue

            self._sync_limits(resp)
            if resp.status_code < 300:
                return resp.json() if resp.content else None

            err = self._parse_error(resp)
            last_exc = err
            if err.code == "timestamp_out_of_window" and err.body.get("server_time_ms"):
                self.clock_offset_ms = int(err.body["server_time_ms"]) - int(time.time() * 1000)
                log.warning("Часы расходятся с сервером на %d ms — корректирую", self.clock_offset_ms)
                continue
            if err.code in ("rate_limited", "banned"):
                wait = err.retry_after or 5
                self.limiter.block(wait + 0.5)
                log.error("%s от сервера (ждать %.1fs). Лимитер должен был это предотвратить!", err.code, wait)
                if err.code == "banned" and attempt >= 1:
                    raise err
                continue
            if err.code in RETRYABLE and attempt < self.max_retries:
                await self._backoff(attempt, err.code, err.retry_after)
                continue
            if err.code in AUTH:
                log.critical("Ошибка ключа: %s", err)
            raise err
        raise last_exc or NetworkError(path)

    async def _backoff(self, attempt: int, why: str, hint: float | None = None):
        wait = hint if hint else min(30.0, 0.5 * 2 ** attempt) * (0.8 + random.random() * 0.4)
        log.warning("Повтор через %.1fs (%s)", wait, why)
        await asyncio.sleep(wait)

    def _sync_limits(self, resp: httpx.Response):
        h = resp.headers

        def num(name):
            try:
                return float(h[name]) if name in h else None
            except ValueError:
                return None
        limit, remaining, reset = num("x-ratelimit-limit"), num("x-ratelimit-remaining"), num("x-ratelimit-reset")
        self.limiter.sync(int(limit) if limit else None, int(remaining) if remaining is not None else None, reset)

    @staticmethod
    def _parse_error(resp: httpx.Response) -> ApiError:
        try:
            body = resp.json()
        except ValueError:
            body = {"error_code": f"http_{resp.status_code}", "error_message": resp.text[:300]}
        code = body.get("error_code") or f"http_{resp.status_code}"
        if resp.status_code >= 500 and code.startswith("http_"):
            code = "internal_error"
        return ApiError(resp.status_code, code, body.get("error_message", ""), body)

    # ------------------------------------------------------------- endpoints
    async def status(self) -> dict:
        return await self.request("GET", "/v1/status")

    async def account(self) -> dict:
        return await self.request("GET", "/v1/account")

    async def instruments(self) -> list[dict]:
        return (await self.request("GET", "/v1/instruments"))["results"]

    async def instrument(self, symbol: str) -> dict:
        return await self.request("GET", f"/v1/instruments/{symbol}")

    async def ticker(self, symbol: str) -> dict:
        return await self.request("GET", f"/v1/tickers/{symbol}")

    async def mark_price(self, symbol: str) -> Decimal:
        t = await self.ticker(symbol)
        if not t.get("mark_price"):
            raise ApiError(0, "no_price", f"нет mark price для {symbol}")
        return Decimal(t["mark_price"])

    async def position(self, symbol: str) -> dict:
        return await self.request("GET", f"/v1/positions/{symbol}")

    async def open_orders(self, symbol: str) -> list[dict]:
        return (await self.request("GET", "/v1/orders", params={"symbol": symbol}))["results"]

    async def order(self, order_id: str) -> dict:
        return await self.request("GET", f"/v1/orders/{order_id}")

    async def order_by_client_id(self, cid: str) -> dict | None:
        res = (await self.request("GET", "/v1/orders", params={"client_order_id": cid}))["results"]
        return res[0] if res else None

    async def place_order(self, body: dict, critical: bool = False) -> dict:
        """Идемпотентно: повтор с тем же client_order_id не создаёт дубль (биржа вернёт существующий)."""
        try:
            return (await self.request("POST", "/v1/orders", body=body, critical=critical))["placed"][0]
        except ApiError as e:
            if e.code == "duplicate_client_order_id" and e.body.get("order"):
                return e.body["order"]
            raise
        except NetworkError:
            if body.get("client_order_id"):
                found = await self.order_by_client_id(body["client_order_id"])
                if found:
                    return found
            raise

    async def cancel_order(self, order_id: str) -> dict:
        return await self.request("DELETE", "/v1/orders", params={"order_id": order_id}, critical=True)

    async def cancel_all(self, symbol: str) -> list[dict]:
        res = await self.request("DELETE", "/v1/orders/batch", params={"symbol": symbol}, critical=True)
        return (res or {}).get("canceled", [])
