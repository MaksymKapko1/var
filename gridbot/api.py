"""Минимальный клиент Variational Omni API (https://docs.variational.io/api-documentation/overview).

Подпись: Ed25519 над 7 строками, соединёнными '\n':
  variational-omni-api/v1, <environment>, <public key>, <timestamp ms>, <METHOD>,
  <path and query>, <sha256(body) hex>
"""
import hashlib
import json
import logging
import time
from decimal import Decimal
from urllib.parse import quote, urlencode

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger("api")

SCHEME = "variational-omni-api/v1"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.body = body or {}


def load_private_key(secret: str) -> Ed25519PrivateKey:
    s = secret.strip().lower().removeprefix("0x")
    raw = bytes.fromhex(s)
    if len(raw) == 64:  # seed + public key
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError("VAR_API_SECRET должен быть 32-байтным Ed25519 приватным ключом в hex")
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_hex(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "0x" + raw.hex()


def canonical_string(environment: str, public_key: str, ts_ms: int, method: str,
                     path_and_query: str, body: bytes) -> str:
    return "\n".join([SCHEME, environment, public_key, str(ts_ms), method.upper(),
                      path_and_query, hashlib.sha256(body).hexdigest()])


class OmniClient:
    def __init__(self, base_url: str, environment: str, api_key: str, api_secret: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.env = environment
        self.key = load_private_key(api_secret)
        derived = public_key_hex(self.key)
        if api_key and api_key.lower() != derived:
            raise SystemExit(f"[config] VAR_API_KEY ({api_key}) не соответствует VAR_API_SECRET (ожидался {derived})")
        self.public_key = derived
        self.timeout = timeout
        self.sesh = requests.Session()
        self.clock_offset_ms = 0
        self.rl_remaining: int | None = None
        self.rl_reset: float | None = None

    # ---------- low level ----------
    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                retries: int = 3):
        qs = ("?" + urlencode({k: v for k, v in params.items() if v is not None}, quote_via=quote)) if params else ""
        path_q = path + qs
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        for attempt in range(retries + 1):
            self._throttle()
            ts = int(time.time() * 1000) + self.clock_offset_ms
            sig = self.key.sign(canonical_string(self.env, self.public_key, ts, method, path_q, data).encode())
            headers = {
                "x-variational-key": self.public_key,
                "x-request-timestamp-ms": str(ts),
                "x-variational-signature": "0x" + sig.hex(),
            }
            if data:
                headers["content-type"] = "application/json"
            resp = self.sesh.request(method, self.base_url + path_q, data=data or None,
                                     headers=headers, timeout=self.timeout)
            self._track_rate_limit(resp)
            if resp.status_code < 300:
                return resp.json() if resp.content else None
            try:
                err = resp.json()
            except ValueError:
                err = {"error_code": "http_error", "error_message": resp.text[:200]}
            code = err.get("error_code", "unknown")
            if code == "timestamp_out_of_window" and err.get("server_time_ms") and attempt < retries:
                self.clock_offset_ms = int(err["server_time_ms"]) - int(time.time() * 1000)
                log.warning("Сдвиг часов %d ms, исправляю", self.clock_offset_ms)
                continue
            if err.get("retry_after_ms") and attempt < retries:
                wait = int(err["retry_after_ms"]) / 1000 + 0.2
                log.warning("%s — жду %.1fs", code, wait)
                time.sleep(wait)
                continue
            raise ApiError(resp.status_code, code, err.get("error_message", ""), err)
        raise ApiError(0, "retries_exhausted", path)

    def _track_rate_limit(self, resp):
        h = resp.headers
        try:
            if "X-RateLimit-Remaining" in h:
                self.rl_remaining = int(h["X-RateLimit-Remaining"])
            if "X-RateLimit-Reset" in h:
                self.rl_reset = float(h["X-RateLimit-Reset"])
        except ValueError:
            pass

    def _throttle(self):
        # бюджет 60 weight/мин; превышения дают "страйки" и бан — не доводим до 429
        if self.rl_remaining is not None and self.rl_remaining <= 5 and self.rl_reset:
            wait = min(max(self.rl_reset - time.time(), 0) + 0.5, 61)
            log.info("Rate limit: осталось %d weight, пауза %.1fs", self.rl_remaining, wait)
            time.sleep(wait)
            self.rl_remaining = None

    # ---------- endpoints ----------
    def status(self) -> dict:
        return self.request("GET", "/v1/status")

    def account(self) -> dict:
        return self.request("GET", "/v1/account")

    def instrument(self, symbol: str) -> dict:
        return self.request("GET", f"/v1/instruments/{symbol}")

    def instruments(self) -> list[dict]:
        return self.request("GET", "/v1/instruments")["results"]

    def ticker(self, symbol: str) -> dict:
        return self.request("GET", f"/v1/tickers/{symbol}")

    def mark_price(self, symbol: str) -> Decimal:
        t = self.ticker(symbol)
        if not t.get("mark_price"):
            raise ApiError(0, "no_price", f"нет mark price для {symbol}")
        return Decimal(t["mark_price"])

    def position(self, symbol: str) -> dict:
        return self.request("GET", f"/v1/positions/{symbol}")

    def open_orders(self, symbol: str) -> list[dict]:
        return self.request("GET", "/v1/orders", params={"symbol": symbol})["results"]

    def order(self, order_id: str) -> dict:
        return self.request("GET", f"/v1/orders/{order_id}")

    def order_by_client_id(self, cid: str) -> dict | None:
        res = self.request("GET", "/v1/orders", params={"client_order_id": cid})["results"]
        return res[0] if res else None

    def place_order(self, body: dict) -> dict:
        """POST /v1/orders. Безопасный повтор: тот же client_order_id."""
        try:
            return self.request("POST", "/v1/orders", body=body)["placed"][0]
        except ApiError as e:
            if e.code == "duplicate_client_order_id" and e.body.get("order"):
                return e.body["order"]
            if e.code in ("outcome_unknown", "request_timeout") and body.get("client_order_id"):
                found = self.order_by_client_id(body["client_order_id"])
                if found:
                    return found
            raise
        except requests.RequestException:
            if body.get("client_order_id"):
                found = self.order_by_client_id(body["client_order_id"])
                if found:
                    return found
            raise

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", "/v1/orders", params={"order_id": order_id})

    def cancel_all(self, symbol: str) -> list:
        return self.request("DELETE", "/v1/orders/batch", params={"symbol": symbol}).get("canceled", [])
