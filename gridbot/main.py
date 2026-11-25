"""Точка входа.

  python -m gridbot.main          # запуск бота
  python -m gridbot.main check    # проверить ключ, инструмент, баланс, цену и выйти
"""
import logging
import signal
import sys
import time

from .api import ApiError, OmniClient
from .bot import GridBot
from .config import Config

log = logging.getLogger("main")
AUTH_ERRORS = ("unknown_key", "invalid_signature", "key_revoked", "key_expired", "insufficient_scope",
               "malformed_credentials", "missing_credentials")
_running = True


def _stop(*_):
    global _running
    log.info("Получен сигнал остановки, завершаюсь...")
    _running = False


def _sleep(sec: float):
    end = time.time() + sec
    while _running and time.time() < end:
        time.sleep(min(0.5, max(end - time.time(), 0)))


def check(cfg: Config, c: OmniClient):
    log.info("Публичный ключ: %s | env=%s | %s", c.public_key, cfg.environment, cfg.base_url)
    log.info("API status: %s", c.status())
    log.info("Аккаунт: %s", c.account())
    syms = [i["symbol"] for i in c.instruments()]
    log.info("Доступные инструменты (%d): %s", len(syms), ", ".join(syms))
    log.info("%s: %s", cfg.symbol, c.instrument(cfg.symbol))
    log.info("Тикер: %s", c.ticker(cfg.symbol))
    log.info("Позиция: %s", c.position(cfg.symbol))
    log.info("Открытые ордера: %s", c.open_orders(cfg.symbol))
    log.info("Остаток rate limit: %s weight", c.rl_remaining)


def main():
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    client = OmniClient(cfg.base_url, cfg.environment, cfg.api_key, cfg.api_secret)
    bot = GridBot(cfg, client)
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            check(cfg, client)
            return
        bot.startup()
    except ApiError as e:
        log.critical("Старт не удался: %s", e)
        if e.code in AUTH_ERRORS:
            log.critical("Ключ не принят. Проверь VAR_API_SECRET/VAR_API_KEY, scope 'trade' и что ключ создан "
                         "в testnet (env=%s). Новый ключ активируется несколько секунд.", cfg.environment)
        if not (len(sys.argv) > 1 and sys.argv[1] == "check"):
            _sleep(60)  # не долбим API при рестартах docker
        sys.exit(2)
    log.info("Бот запущен: %s, %d уровней, size=%s, dry_run=%s", cfg.symbol, cfg.grid_levels, bot.size, cfg.dry_run)
    errors = 0
    while _running:
        try:
            bot.tick()
            errors = 0
        except ApiError as e:
            errors += 1
            log.error("Ошибка API (%d подряд): %s", errors, e)
            if e.code in AUTH_ERRORS:
                log.critical("Проблема с ключом — проверь VAR_API_KEY/VAR_API_SECRET и scope 'trade'")
                break
        except Exception as e:  # noqa: BLE001 — сетевые сбои не должны валить бота
            errors += 1
            log.error("Ошибка цикла (%d подряд): %r", errors, e)
        if errors >= 20:
            log.critical("Слишком много ошибок подряд — выхожу (docker перезапустит)")
            break
        _sleep(cfg.poll_interval * (1 + min(errors, 4)))
    bot.shutdown()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
