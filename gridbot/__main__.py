"""CLI.

  python -m gridbot [run]   — запуск бота (по умолчанию)
  python -m gridbot check   — проверить ключ, баланс, инструмент, цену и выйти
  python -m gridbot status  — состояние бота + позиция + открытые ордера
  python -m gridbot cancel  — снять все ордера на SYMBOL
  python -m gridbot close   — снять ордера + закрыть позицию по рынку + режим СТОП
  python -m gridbot resume  — снять режим СТОП (после перезапуска сетка построится заново)

cancel/close/resume запускай при ОСТАНОВЛЕННОМ боте (make stop), иначе он перезапишет состояние.
"""
import asyncio
import logging
import signal
import sys
import time

from .config import Config, ConfigError
from .core.engine import AuthFailure, GridEngine
from .core.orders import FatalError, OrderManager
from .core.state import StateStore
from .core.watchdog import Watchdog
from .exchange import ApiError, OmniClient, Signer, WeightLimiter
from .strategy.grid import make_levels

log = logging.getLogger("main")

# exit codes: 0 — штатно, 1 — сбой (docker перезапустит), 2 — конфиг/ключ (перезапуск с задержкой)
FATAL_DELAY_SEC = 120


def setup_logging(level: str):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
                        stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def make_client(cfg: Config) -> OmniClient:
    signer = Signer(cfg.api_secret, cfg.environment, cfg.api_key)
    return OmniClient(cfg.base_url, signer, WeightLimiter(cfg.rate_limit, reserve=cfg.rate_reserve))


async def cmd_run(cfg: Config) -> int:
    client = make_client(cfg)
    engine = GridEngine(cfg, client, StateStore(cfg.state_file))
    Watchdog(lambda: engine.last_beat, cfg.watchdog_sec, cfg.heartbeat_file).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: (log.info("Сигнал %s — останавливаюсь", s.name), stop.set()))

    code = 0
    try:
        await engine.run(stop)
    except AuthFailure as e:
        log.critical("Ключ не принят: %s. Проверь VAR_API_SECRET, scope 'trade', testnet. Повтор через %ds",
                     e, FATAL_DELAY_SEC)
        await _sleep_unless(stop, FATAL_DELAY_SEC)
        code = 2
    except FatalError as e:
        log.critical("Фатальная ошибка: %s", e)
        await _sleep_unless(stop, 30)
        code = 1
    except Exception as e:  # noqa: BLE001
        log.exception("Сбой: %r — контейнер будет перезапущен", e)
        await _sleep_unless(stop, 10)
        code = 1
    finally:
        await engine.shutdown()
        await client.aclose()
    return code


async def _sleep_unless(stop: asyncio.Event, sec: float):
    try:
        await asyncio.wait_for(stop.wait(), timeout=sec)
    except asyncio.TimeoutError:
        pass


async def cmd_check(cfg: Config) -> int:
    c = make_client(cfg)
    try:
        log.info("Ключ: %s | env=%s | %s", c.signer.public_key, cfg.environment, cfg.base_url)
        log.info("API: %s", await c.status())
        log.info("Аккаунт: %s", await c.account())
        syms = [i["symbol"] for i in await c.instruments()]
        log.info("Инструментов: %d (%s ...)", len(syms), ", ".join(syms[:30]))
        log.info("%s: %s", cfg.symbol, await c.instrument(cfg.symbol))
        log.info("Тикер: %s", await c.ticker(cfg.symbol))
        log.info("Позиция: %s", await c.position(cfg.symbol))
        log.info("Открытые ордера: %d", len(await c.open_orders(cfg.symbol)))
        # проверка scope trade: отмена ордеров требует 'trade'; на пустом символе ничего не меняет
        if not await c.open_orders(cfg.symbol):
            try:
                await c.cancel_all(cfg.symbol)
                log.info("Scope 'trade': OK")
            except ApiError as e:
                log.error("Scope 'trade' НЕТ: %s — создай ключ с правом trade", e.code)
                return 2
        return 0
    finally:
        await c.aclose()


async def cmd_status(cfg: Config) -> int:
    st = StateStore(cfg.state_file).load()
    if st:
        log.info("Режим: %s %s | ордеров %d | очередь %d | %s", st.mode, st.halt_reason, len(st.orders),
                 len(st.pending), st.stats)
        for p in st.pending:
            if p.last_error:
                log.info("  в очереди L%d %s: попыток %d, ошибка %s", p.level, p.side, p.attempts, p.last_error)
    else:
        log.info("Состояния нет")
    c = make_client(cfg)
    try:
        log.info("Позиция: %s", await c.position(cfg.symbol))
        for o in await c.open_orders(cfg.symbol):
            log.info("  %s %s %s @ %s (%s)", o["status"], o["side"], o.get("size"), o.get("price"),
                     o.get("client_order_id"))
    finally:
        await c.aclose()
    return 0


async def _manual(cfg: Config, close: bool) -> int:
    c = make_client(cfg)
    store = StateStore(cfg.state_file)
    from .core.state import BotState
    st = store.load() or BotState()
    from decimal import Decimal
    levels = ([Decimal(x) for x in st.levels] if st.levels else
              make_levels(cfg.grid_lower, cfg.grid_upper, cfg.grid_levels) if not cfg.auto else [Decimal(1), Decimal(2)])
    om = OrderManager(cfg, c, st, levels, lambda: store.save(st))
    try:
        await om.cancel_all()
        if close:
            ok = await om.flatten()
            st.mode, st.halt_reason = "halted", "закрыто вручную (close)"
            store.save(st)
            return 0 if ok else 1
        return 0
    finally:
        await c.aclose()


def cmd_resume(cfg: Config) -> int:
    store = StateStore(cfg.state_file)
    st = store.load()
    if not st:
        log.info("Состояния нет — нечего возобновлять")
        return 0
    st.mode, st.halt_reason = "running", ""
    st.orders.clear()
    st.pending.clear()
    store.save(st)
    log.info("Режим СТОП снят. При следующем запуске сетка будет построена заново.")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        cfg = Config.from_env()
    except (ConfigError, ValueError) as e:
        setup_logging("INFO")
        log.critical("Ошибка конфигурации: %s", e)
        if cmd == "run":
            time.sleep(FATAL_DELAY_SEC)  # не крутить рестарты docker вхолостую
        return 2
    setup_logging(cfg.log_level)
    try:
        if cmd == "run":
            return asyncio.run(cmd_run(cfg))
        if cmd == "check":
            return asyncio.run(cmd_check(cfg))
        if cmd == "status":
            return asyncio.run(cmd_status(cfg))
        if cmd == "cancel":
            return asyncio.run(_manual(cfg, close=False))
        if cmd == "close":
            return asyncio.run(_manual(cfg, close=True))
        if cmd == "resume":
            return cmd_resume(cfg)
    except ValueError as e:  # битый ключ и т.п.
        log.critical("%s", e)
        return 2
    except ApiError as e:
        log.critical("%s", e)
        return 2
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main())
