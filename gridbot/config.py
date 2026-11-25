"""Конфиг бота: всё читается из переменных окружения (.env)."""
import os
from dataclasses import dataclass
from decimal import Decimal

TESTNET_URL = "https://api.testnet.variational.io/v1"


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise SystemExit(f"[config] переменная окружения {name} не задана (см. .env.example)")
    return val or ""


def _bool(name: str, default: str = "false") -> bool:
    return _get(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    base_url: str
    wallet_private_key: str  # нужен для подписи permit на депозит маржи в пул

    underlying: str
    instrument_type: str  # perpetual_future | spot
    settlement_asset: str

    grid_lower: Decimal
    grid_upper: Decimal
    grid_levels: int
    order_qty: Decimal

    poll_interval: float
    rfq_ttl_sec: int
    quote_wait_sec: float
    max_slippage_pct: Decimal
    dry_run: bool
    state_file: str
    log_level: str

    @staticmethod
    def from_env() -> "Config":
        cfg = Config(
            api_key=_get("VAR_API_KEY", required=True),
            api_secret=_get("VAR_API_SECRET", required=True),
            base_url=_get("VAR_BASE_URL", TESTNET_URL),
            wallet_private_key=_get("WALLET_PRIVATE_KEY", ""),
            underlying=_get("UNDERLYING", "ETH").upper(),
            instrument_type=_get("INSTRUMENT_TYPE", "perpetual_future"),
            settlement_asset=_get("SETTLEMENT_ASSET", "USDC"),
            grid_lower=Decimal(_get("GRID_LOWER", required=True)),
            grid_upper=Decimal(_get("GRID_UPPER", required=True)),
            grid_levels=int(_get("GRID_LEVELS", "10")),
            order_qty=Decimal(_get("ORDER_QTY", required=True)),
            poll_interval=float(_get("POLL_INTERVAL_SEC", "5")),
            rfq_ttl_sec=int(_get("RFQ_TTL_SEC", "30")),
            quote_wait_sec=float(_get("QUOTE_WAIT_SEC", "10")),
            max_slippage_pct=Decimal(_get("MAX_SLIPPAGE_PCT", "0.5")),
            dry_run=_bool("DRY_RUN", "true"),
            state_file=_get("STATE_FILE", "/data/state.json"),
            log_level=_get("LOG_LEVEL", "INFO"),
        )
        if cfg.grid_lower >= cfg.grid_upper:
            raise SystemExit("[config] GRID_LOWER должен быть меньше GRID_UPPER")
        if cfg.grid_levels < 2:
            raise SystemExit("[config] GRID_LEVELS должен быть >= 2")
        if cfg.order_qty <= 0:
            raise SystemExit("[config] ORDER_QTY должен быть > 0")
        return cfg

    @property
    def instrument(self) -> dict:
        inst = {
            "instrument_type": self.instrument_type,
            "underlying": self.underlying,
            "settlement_asset": self.settlement_asset,
            "dex_token_details": None,
        }
        if self.instrument_type == "perpetual_future":
            inst["funding_interval_s"] = 3600
        return inst
