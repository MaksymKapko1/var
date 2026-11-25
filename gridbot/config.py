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
        val = val.strip()
        if val.startswith("#"):  # пустое значение с комментарием: docker compose отдаёт сам комментарий
            val = ""
        val = val.split(" #")[0].split("\t#")[0].strip().strip('"').strip("'")
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


def _legs(raw: str) -> tuple[tuple[str, str], ...]:
    legs = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        sym, _, side = part.partition(":")
        side = side.strip().lower()
        side = {"buy": "long", "sell": "short"}.get(side, side)
        if side not in ("long", "short"):
            raise ConfigError(f"ROTATION_LEGS: '{part}' — нужно SYMBOL:long или SYMBOL:short")
        sym = sym.strip().upper()
        if "-" not in sym:
            sym += "-PERP"
        legs.append((sym, side))
    if len({s for s, _ in legs}) != len(legs):
        raise ConfigError("ROTATION_LEGS: символы повторяются")
    return tuple(legs)


@dataclass(frozen=True)
class Config:
    # --- доступ ---
    api_key: str
    api_secret: str
    base_url: str
    environment: str
    # --- сетка ---
    symbol: str
    grid_lower: Decimal | None
    grid_upper: Decimal | None
    grid_step_pct: Decimal | None     # авто-сетка вокруг цены (тогда LOWER/UPPER не нужны)
    grid_levels: int
    order_size: Decimal | None
    balance_pct: Decimal | None
    trigger_by: str
    max_slippage: Decimal | None
    # --- риск ---
    range_exit_pct: Decimal          # насколько цена должна выйти за сетку, чтобы сработал RANGE_EXIT_ACTION
    range_exit_action: str           # recenter | pause | close | none
    recenter_flatten: bool           # при перестройке закрывать позицию по рынку
    recenter_min_sec: float          # не перестраивать чаще, чем раз в N секунд
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
    # --- стратегия ---
    strategy: str = "grid"                                   # grid | rotation
    rotation_legs: tuple[tuple[str, str], ...] = ()          # ((symbol, long|short), ...)
    rotation_margin_usdc: Decimal = Decimal(100)             # маржа на одну ногу
    rotation_notional_usdc: Decimal | None = None            # если задан — объём позиции напрямую (без плеча)
    rotation_interval_min: float = 60                        # переоткрытие раз в N минут
    rotation_check_sec: float = 60                           # как часто проверять, что все ноги открыты
    rotation_max_slippage: Decimal = Decimal("0.01")

    @property
    def auto(self) -> bool:
        return self.grid_step_pct is not None

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
            grid_lower=_opt_dec("GRID_LOWER"),
            grid_upper=_opt_dec("GRID_UPPER"),
            grid_step_pct=_opt_dec("GRID_STEP_PCT"),
            grid_levels=int(_dec("GRID_LEVELS", "10")),
            order_size=_opt_dec("ORDER_SIZE"),
            balance_pct=_opt_dec("GRID_BALANCE_PCT"),
            trigger_by=_get("TRIGGER_BY", "quote"),
            max_slippage=_opt_dec("MAX_SLIPPAGE", "0.02"),
            range_exit_pct=_dec("RANGE_EXIT_PCT", "0" if _get("GRID_STEP_PCT", "") else "3"),
            range_exit_action=_get("RANGE_EXIT_ACTION", "recenter" if _get("GRID_STEP_PCT", "") else "pause").lower(),
            recenter_flatten=_bool("RECENTER_FLATTEN", "false"),
            recenter_min_sec=_float("RECENTER_MIN_SEC", "60"),
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
            strategy=_get("STRATEGY", "grid").lower(),
            rotation_legs=_legs(_get("ROTATION_LEGS", "BTC-PERP:long,ETH-PERP:short,HYPE-PERP:long,SOL-PERP:short")),
            rotation_margin_usdc=_dec("ROTATION_MARGIN_USDC", "100"),
            rotation_notional_usdc=_opt_dec("ROTATION_NOTIONAL_USDC"),
            rotation_interval_min=_float("ROTATION_INTERVAL_MIN", "60"),
            rotation_check_sec=_float("ROTATION_CHECK_SEC", "60"),
            rotation_max_slippage=_dec("ROTATION_MAX_SLIPPAGE", "0.01"),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if self.strategy not in ("grid", "rotation"):
            raise ConfigError("STRATEGY: grid | rotation")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,10}", self.bot_id):
            raise ConfigError("BOT_ID: 1-10 символов [A-Za-z0-9_]")
        if not 0 <= self.rate_reserve < self.rate_limit:
            raise ConfigError("RATE_LIMIT_RESERVE должен быть меньше RATE_LIMIT_PER_MIN")
        if self.strategy == "rotation":
            if not self.rotation_legs:
                raise ConfigError("ROTATION_LEGS пуст: формат BTC-PERP:long,ETH-PERP:short")
            if self.rotation_margin_usdc <= 0 and not self.rotation_notional_usdc:
                raise ConfigError("ROTATION_MARGIN_USDC должен быть > 0")
            if self.rotation_interval_min < 5:
                raise ConfigError("ROTATION_INTERVAL_MIN >= 5")
            if self.rotation_check_sec < 15:
                raise ConfigError("ROTATION_CHECK_SEC >= 15 (лимит API)")
            if not 0 < self.rotation_max_slippage <= 1:
                raise ConfigError("ROTATION_MAX_SLIPPAGE: доля (0, 1], например 0.01")
            return
        if self.auto:
            if not 0 < self.grid_step_pct <= 20:
                raise ConfigError("GRID_STEP_PCT: шаг сетки в %, (0, 20], например 0.15")
        else:
            if self.grid_lower is None or self.grid_upper is None:
                raise ConfigError("задай GRID_STEP_PCT (авто-сетка вокруг цены) или GRID_LOWER + GRID_UPPER")
            if self.grid_lower <= 0 or self.grid_lower >= self.grid_upper:
                raise ConfigError("нужно 0 < GRID_LOWER < GRID_UPPER")
            if self.range_exit_action == "recenter":
                raise ConfigError("RANGE_EXIT_ACTION=recenter работает только с GRID_STEP_PCT (авто-сетка)")
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
        if self.range_exit_action not in ("recenter", "pause", "close", "none"):
            raise ConfigError("RANGE_EXIT_ACTION: recenter | pause | close | none")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,10}", self.bot_id):
            raise ConfigError("BOT_ID: 1-10 символов [A-Za-z0-9_]")
        if self.poll_interval < 3:
            raise ConfigError("POLL_INTERVAL_SEC >= 3 (лимит API 60 weight/мин)")
        if not 0 <= self.rate_reserve < self.rate_limit:
            raise ConfigError("RATE_LIMIT_RESERVE должен быть меньше RATE_LIMIT_PER_MIN")
