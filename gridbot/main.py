"""Точка входа.

  python -m gridbot.main          # запуск бота
  python -m gridbot.main check    # проверить ключи, цену, баланс и выйти
"""
import logging
import signal
import sys
import time
from decimal import Decimal

from .config import Config
from .exchange import Exchange, TradeFailed
from .grid import Grid

log = logging.getLogger("bot")
_running = True


def _stop(*_):
    global _running
    log.info("Получен сигнал остановки, завершаюсь...")
    _running = False


def check(cfg: Config, ex: Exchange):
    me = ex.me()
    log.info("API ключ OK: key_id=%s company=%s role=%s", me["key_id"], me["company_id"], me["role"])
    if me["role"] != "writer":
        log.warning("Роль ключа '%s' — торговать нельзя, нужен ключ с ролью writer", me["role"])
    log.info("Баланс/портфель: %s", ex.summary())
    log.info("Лимиты: %s", ex.limits())
    log.info("Mark price %s: %s", cfg.underlying, ex.mark_price())
    log.info("Текущая позиция %s: %s", cfg.underlying, ex.position_qty())
    ex.lp_companies()


def run(cfg: Config, ex: Exchange):
    grid = Grid(cfg.grid_lower, cfg.grid_upper, cfg.grid_levels, cfg.state_file)
    log.info("Грид %s %s: %s | qty=%s | dry_run=%s",
             cfg.underlying, cfg.instrument_type, grid.describe(), cfg.order_qty, cfg.dry_run)
    errors = 0
    while _running:
        try:
            price = ex.mark_price()
            acts = grid.actions_for_price(price)
            log.debug("price=%s filled=%s actions=%d", price, sorted(grid.filled), len(acts))
            for act in acts:
                if not _running:
                    break
                log.info(">>> %s cell=%d level=%s price=%s qty=%s", act.side.upper(), act.cell,
                         round(act.level_price, 4), price, cfg.order_qty)
                if cfg.dry_run:
                    fill = price
                else:
                    try:
                        fill = ex.market_trade(act.side, cfg.order_qty, price)
                    except TradeFailed as e:
                        log.warning("Сделка не выполнена: %s", e)
                        break  # попробуем на следующем тике
                grid.mark_done(act)
                log.info("<<< %s исполнено по %s | занятые ячейки: %s", act.side, fill, sorted(grid.filled))
            errors = 0
        except Exception as e:  # noqa: BLE001 — бот не должен падать от сетевых ошибок
            errors += 1
            log.error("Ошибка цикла (%d подряд): %r", errors, e)
            if errors >= 20:
                log.critical("Слишком много ошибок подряд — выхожу (docker перезапустит)")
                sys.exit(1)
        _sleep(cfg.poll_interval)


def _sleep(sec: float):
    end = time.time() + sec
    while _running and time.time() < end:
        time.sleep(min(0.5, end - time.time()))


def main():
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    ex = Exchange(cfg)
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check(cfg, ex)
        return
    check(cfg, ex)
    run(cfg, ex)


if __name__ == "__main__":
    main()
