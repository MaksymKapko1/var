"""Конфиг бота: всё читается из переменных окружения (.env)."""
import os
import re
from dataclasses import dataclass
from decimal import Decimal

TESTNET_URL = "https://api.omni.testnet.variational.io"


class ConfigError(Exception):
    pass


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if val is not None:
        val = val.split(" #")[0].strip().strip('"').strip("'")  # допускаем комментарии/кавычки в .env
    if required and not val:
        raise ConfigError(f"переменная окружения {name} не задана (см. .env.example)")
    return val or ""


def _bool(name: str, default: str) -> bool:
    return _get(name, default).lower() in ("1", "true", "yes", "y", "on")


def _dec(name: str, default: str, required: bool = False) -> Decimal:
    v = _get(name, default, required)
    try:
        return Decimal(v)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"{name}={v!r} — не число") from e


def _opt_dec(name: str, default: str = "") -> Decimal | None:
    v = _get(name, default).lower()
    return None if v in ("", "none", "null", "off") else _dec(name, default)


def _float(name: str, default: str) -> float:
    return float(_dec(name, default))


@dataclass(frozen=True)
class Config:
    # --- доступ ---
    api_key: str
    api_secret: str
    base_url: str
    environment: str
    # --- сетка ---
    symbol: str
    grid_lower: Decimal
    grid_upper: Decimal
    grid_levels: int
    order_size: Decimal | None
    balance_pct: Decimal | None
    trigger_by: str
    max_slippage: Decimal | None
    # --- риск ---
    range_exit_pct: Decimal          # насколько цена должна выйти за сетку, чтобы сработал RANGE_EXIT_ACTION
    range_exit_action: str           # pause | close | none
    max_loss_usdc: Decimal | None    # стоп по суммарному PnL позиции -> закрыть и остановиться
    max_position_mult: Decimal       # аварийный стоп, если позиция > mult * (уровни-1) * size
    close_max_slippage: Decimal
    cancel_on_exit: bool
    close_on_exit: bool
    # --- работа ---
    bot_id: str
    poll_interval: float
    risk_interval: float
    rate_limit: int
    rate_reserve: int
    watchdog_sec: float
    dry_run: bool
    data_dir: str
    log_level: str

    @property
    def state_file(self) -> str:
        return os.path.join(self.data_dir, "state.json")

    @property
    def heartbeat_file(self) -> str:
        return os.path.join(self.data_dir, "heartbeat")

    @staticmethod
    def from_env() -> "Config":
        cfg = Config(
            api_key=_get("VAR_API_KEY", ""),
            api_secret=_get("VAR_API_SECRET", required=True),
            base_url=_get("VAR_BASE_URL", TESTNET_URL),
            environment=_get("VAR_ENVIRONMENT", "testnet"),
            symbol=_get("SYMBOL", "ETH-PERP").upper(),
            grid_lower=_dec("GRID_LOWER", "", required=True),
            grid_upper=_dec("GRID_UPPER", "", required=True),
            grid_levels=int(_dec("GRID_LEVELS", "10")),
            order_size=_opt_dec("ORDER_SIZE"),
            balance_pct=_opt_dec("GRID_BALANCE_PCT"),
            trigger_by=_get("TRIGGER_BY", "quote"),
            max_slippage=_opt_dec("MAX_SLIPPAGE", "0.02"),
            range_exit_pct=_dec("RANGE_EXIT_PCT", "3"),
            range_exit_action=_get("RANGE_EXIT_ACTION", "pause").lower(),
            max_loss_usdc=_opt_dec("MAX_LOSS_USDC"),
            max_position_mult=_dec("MAX_POSITION_MULT", "1.5"),
            close_max_slippage=_dec("CLOSE_MAX_SLIPPAGE", "0.05"),
            cancel_on_exit=_bool("CANCEL_ON_EXIT", "false"),
            close_on_exit=_bool("CLOSE_ON_EXIT", "false"),
            bot_id=_get("BOT_ID", "grid"),
            poll_interval=_float("POLL_INTERVAL_SEC", "15"),
            risk_interval=_float("RISK_CHECK_SEC", "30"),
            rate_limit=int(_dec("RATE_LIMIT_PER_MIN", "60")),
            rate_reserve=int(_dec("RATE_LIMIT_RESERVE", "8")),
            watchdog_sec=_float("WATCHDOG_SEC", "300"),
            dry_run=_bool("DRY_RUN", "true"),
            data_dir=_get("DATA_DIR", "/data"),
            log_level=_get("LOG_LEVEL", "INFO").upper(),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if self.grid_lower <= 0 or self.grid_lower >= self.grid_upper:
            raise ConfigError("нужно 0 < GRID_LOWER < GRID_UPPER")
        if not 3 <= self.grid_levels <= 50:
            raise ConfigError("GRID_LEVELS: от 3 до 50 (лимит Omni — 50 ордеров на инструмент)")
        if (self.order_size is None) == (self.balance_pct is None):
            raise ConfigError("задай РОВНО ОДНО: ORDER_SIZE (монет на ордер) или GRID_BALANCE_PCT (% equity на сетку)")
        if self.order_size is not None and self.order_size <= 0:
            raise ConfigError("ORDER_SIZE должен быть > 0")
        if self.balance_pct is not None and not 0 < self.balance_pct <= 1000:
            raise ConfigError("GRID_BALANCE_PCT: (0, 1000]")
        if self.trigger_by not in ("quote", "mark"):
            raise ConfigError("TRIGGER_BY: quote или mark")
        if self.max_slippage is not None and not 0 < self.max_slippage <= 1:
            raise ConfigError("MAX_SLIPPAGE: доля (0, 1], например 0.02")
        if not 0 < self.close_max_slippage <= 1:
            raise ConfigError("CLOSE_MAX_SLIPPAGE: доля (0, 1]")
        if self.range_exit_action not in ("pause", "close", "none"):
            raise ConfigError("RANGE_EXIT_ACTION: pause | close | none")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,10}", self.bot_id):
            raise ConfigError("BOT_ID: 1-10 символов [A-Za-z0-9_]")
        if self.poll_interval < 3:
            raise ConfigError("POLL_INTERVAL_SEC >= 3 (лимит API 60 weight/мин)")
        if not 0 <= self.rate_reserve < self.rate_limit:
            raise ConfigError("RATE_LIMIT_RESERVE должен быть меньше RATE_LIMIT_PER_MIN")
