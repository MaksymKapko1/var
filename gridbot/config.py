"""Конфиг бота: всё читается из переменных окружения (.env)."""
import os
import re
from dataclasses import dataclass
from decimal import Decimal

TESTNET_URL = "https://api.omni.testnet.variational.io"


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if val is not None:
        val = val.split(" #")[0].strip()  # допускаем комментарии в .env после значения
    if required and not val:
        raise SystemExit(f"[config] переменная окружения {name} не задана (см. .env.example)")
    return val or ""


def _bool(name: str, default: str) -> bool:
    return _get(name, default).lower() in ("1", "true", "yes", "y", "on")


def _opt_decimal(name: str, default: str) -> Decimal | None:
    v = _get(name, default).lower()
    return None if v in ("", "none", "null") else Decimal(v)


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    base_url: str
    environment: str

    symbol: str
    grid_lower: Decimal
    grid_upper: Decimal
    grid_levels: int
    order_size: Decimal | None
    balance_pct: Decimal | None
    trigger_by: str
    max_slippage: Decimal | None

    bot_id: str
    poll_interval: float
    dry_run: bool
    cancel_on_exit: bool
    state_file: str
    log_level: str

    @staticmethod
    def from_env() -> "Config":
        cfg = Config(
            api_key=_get("VAR_API_KEY", ""),
            api_secret=_get("VAR_API_SECRET", required=True),
            base_url=_get("VAR_BASE_URL", TESTNET_URL),
            environment=_get("VAR_ENVIRONMENT", "testnet"),
            symbol=_get("SYMBOL", "ETH-PERP").upper(),
            grid_lower=Decimal(_get("GRID_LOWER", required=True)),
            grid_upper=Decimal(_get("GRID_UPPER", required=True)),
            grid_levels=int(_get("GRID_LEVELS", "10")),
            order_size=_opt_decimal("ORDER_SIZE", ""),
            balance_pct=_opt_decimal("GRID_BALANCE_PCT", ""),
            trigger_by=_get("TRIGGER_BY", "quote"),
            max_slippage=_opt_decimal("MAX_SLIPPAGE", "0.02"),
            bot_id=_get("BOT_ID", "grid"),
            poll_interval=float(_get("POLL_INTERVAL_SEC", "15")),
            dry_run=_bool("DRY_RUN", "true"),
            cancel_on_exit=_bool("CANCEL_ON_EXIT", "true"),
            state_file=_get("STATE_FILE", "/data/state.json"),
            log_level=_get("LOG_LEVEL", "INFO"),
        )
        if cfg.grid_lower >= cfg.grid_upper:
            raise SystemExit("[config] GRID_LOWER должен быть меньше GRID_UPPER")
        if not 3 <= cfg.grid_levels <= 50:
            raise SystemExit("[config] GRID_LEVELS: от 3 до 50 (лимит Omni — 50 ордеров на инструмент)")
        if (cfg.order_size is None) == (cfg.balance_pct is None):
            raise SystemExit("[config] задай РОВНО ОДНО: ORDER_SIZE (монеты на ордер) или GRID_BALANCE_PCT (% equity на всю сетку)")
        if cfg.order_size is not None and cfg.order_size <= 0:
            raise SystemExit("[config] ORDER_SIZE должен быть > 0")
        if cfg.balance_pct is not None and not 0 < cfg.balance_pct <= 1000:
            raise SystemExit("[config] GRID_BALANCE_PCT: от 0 до 1000 (больше 100 = с плечом)")
        if cfg.trigger_by not in ("quote", "mark"):
            raise SystemExit("[config] TRIGGER_BY: quote или mark")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,10}", cfg.bot_id):
            raise SystemExit("[config] BOT_ID: 1-10 символов [A-Za-z0-9_]")
        return cfg
