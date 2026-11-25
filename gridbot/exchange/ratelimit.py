"""Лимитер по весу запросов (Omni: 60 weight / скользящая минута, за 429 — страйки и баны).

Стратегия — никогда не получать 429:
  * локально считаем вес в скользящем окне 60с;
  * синхронизируемся по заголовкам X-RateLimit-Remaining/Reset (сервер — истина);
  * держим резерв: обычные запросы не трогают последние `reserve` weight,
    критичные (отмена ордеров, закрытие позиции) — могут.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

log = logging.getLogger("ratelimit")


class WeightLimiter:
    def __init__(self, limit: int = 60, window: float = 60.0, reserve: int = 6,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.limit = limit
        self.window = window
        self.reserve = reserve
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, float]] = deque()  # (time, weight)
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def _purge(self, now: float):
        while self._events and self._events[0][0] <= now - self.window:
            self._events.popleft()

    def used(self) -> float:
        self._purge(self._clock())
        return sum(w for _, w in self._events)

    async def acquire(self, weight: float = 1, critical: bool = False):
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                self._purge(now)
                cap = self.limit - (0 if critical else self.reserve)
                if sum(w for _, w in self._events) + weight <= cap:
                    self._events.append((now, weight))
                    return
                wait = self._events[0][0] + self.window - now + 0.05 if self._events else 1.0
                log.debug("Бюджет исчерпан (%.0f/%d), жду %.1fs", self.used(), self.limit, wait)
                await self._sleep(max(wait, 0.05))

    def sync(self, limit: int | None, remaining: int | None, reset_unix: float | None):
        """Подгоняем локальный учёт под заголовки сервера (только в сторону осторожности)."""
        if limit:
            self.limit = limit
        if remaining is None:
            return
        now = self._clock()
        self._purge(now)
        local_remaining = self.limit - sum(w for _, w in self._events)
        if remaining < local_remaining:
            self._events.append((now, local_remaining - remaining))
        if remaining <= 0 and reset_unix:
            self.block(max(reset_unix - time.time(), 0) + 0.5)

    def block(self, seconds: float):
        until = self._clock() + seconds
        if until > self._blocked_until:
            self._blocked_until = until
            log.warning("Запросы на паузе %.1fs (rate limit)", seconds)
