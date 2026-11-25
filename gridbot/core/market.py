"""Рыночные операции, общие для стратегий: ожидание ордера, закрытие позиции, открытие по рынку."""
import asyncio
import logging
import secrets
import time
from decimal import Decimal
from typing import Callable

from ..exchange import ApiError, NetworkError, OmniClient
from ..strategy.precision import fmt

log = logging.getLogger("market")

LIVE = ("open", "queued", "untriggered")


def make_cid(prefix: str) -> str:
    """client_order_id: уникален навсегда (требование API), <= 36 символов."""
    return f"{prefix}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


async def wait_final(c: OmniClient, oid: str, timeout: float = 30) -> dict:
    end = time.time() + timeout
    o = await c.order(oid)
    while o.get("status") in LIVE and time.time() < end:
        await asyncio.sleep(2)
        o = await c.order(oid)
    return o


async def flatten_symbol(c: OmniClient, symbol: str, base_slippage: Decimal, cid: Callable[[], str],
                         attempts: int = 6) -> bool:
    """Закрыть позицию по рынку reduce-only. True — позиция ноль."""
    for i in range(attempts):
        pos = await c.position(symbol)
        size = Decimal(pos.get("size") or "0")
        if size == 0:
            if i:
                log.info("Позиция %s закрыта", symbol)
            return True
        side = "sell" if size > 0 else "buy"
        slip = min(base_slippage * (1 + i), Decimal("0.2"))  # расширяем с каждой попыткой
        body = {"type": "market", "symbol": symbol, "side": side, "size": fmt(abs(size)),
                "max_slippage": fmt(slip), "reduce_only": True, "client_order_id": cid()}
        log.warning("ЗАКРЫТИЕ %s %s: %s %s по рынку (slippage до %s)", symbol, size, side, fmt(abs(size)), slip)
        try:
            o = await c.place_order(body, critical=True)
            o = await wait_final(c, o["order_id"])
            log.info("Закрывающий ордер %s: %s %s", symbol, o.get("status"), o.get("reason") or o.get("fill_price"))
            if o.get("status") == "filled":
                continue  # перепроверим позицию сразу
        except (ApiError, NetworkError) as e:
            log.error("Закрытие %s не удалось: %s", symbol, e)
        await asyncio.sleep(min(3 * (i + 1), 15))
    log.critical("Не удалось закрыть позицию %s за %d попыток — закрой вручную!", symbol, attempts)
    return False
