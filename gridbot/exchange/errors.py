"""Ошибки API и их классификация (коды из ErrorCode спецификации Omni)."""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", body: dict | None = None):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body or {}

    @property
    def retry_after(self) -> float | None:
        ms = self.body.get("retry_after_ms")
        return ms / 1000 if ms else None


class NetworkError(Exception):
    """Сеть/таймаут: результат запроса неизвестен."""


# Временные проблемы: можно повторить тот же запрос (с новой подписью).
RETRYABLE = {
    "rate_limited", "overloaded", "service_unavailable", "request_timeout", "internal_error",
    "outcome_unknown", "risk_check_unavailable", "market_status_unavailable",
}

# Ключ не работает: торговать нельзя, нужен человек.
AUTH = {
    "missing_credentials", "malformed_credentials", "unknown_key", "invalid_signature",
    "key_revoked", "key_expired", "insufficient_scope", "account_not_found", "account_pending",
}

# Ошибки размещения ордера -> (пауза перед повтором в секундах, перечитать инструмент?)
PLACE_ERRORS: dict[str, tuple[float, bool]] = {
    "invalid_price_increment": (5, True),
    "invalid_size_increment": (5, True),
    "below_min_notional": (300, True),
    "above_max_notional": (600, False),
    "market_not_trading": (60, True),
    "market_status_unavailable": (30, False),
    "market_limit_exceeded": (300, False),
    "size_exceeds_liquidity": (120, False),
    "quote_unavailable": (30, False),
    "too_many_orders": (300, False),
    "conflicting_order": (600, False),
    "instrument_not_permitted": (3600, False),
    "instrument_not_supported": (3600, False),
    "validation_failed": (600, True),
    "invalid_body": (600, False),
    "unknown_symbol": (3600, False),
}

# RejectReason (ордер принят, но биржа отклонила при исполнении) -> базовая пауза, сек.
REJECT_BACKOFF: dict[str, float] = {
    "quote_unavailable": 10,
    "price_check_failed": 10,
    "slippage_exceeded": 15,
    "settlement_failed": 30,
    "internal_error": 30,
    "market_not_trading": 60,
    "trading_disabled": 300,
    "invalid_order": 60,
    "insufficient_margin": 300,
    "account_limit_exceeded": 600,
    "leverage_limit_exceeded": 600,
    "market_limit_exceeded": 300,
    "reduce_only_violated": 60,
}
