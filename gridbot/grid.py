"""Логика нейтрального грида на лимитных ордерах (не зависит от сети — легко тестировать).

Уровни L0 < L1 < ... < L(N-1) делят диапазон [GRID_LOWER, GRID_UPPER].
Старт: ниже цены ставим BUY-лимитки, выше — SELL-лимитки, ближайший к цене уровень пустой.
Дальше:
  * BUY на уровне i исполнился  -> ставим SELL на уровне i+1
  * SELL на уровне i исполнился -> ставим BUY  на уровне i-1
Каждая пара buy(i) -> sell(i+1) приносит ~ шаг * size.
В любой момент на сетке N-1 ордер, позиция в пределах ±(N-1)*size.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Slot:
    level: int
    side: str  # "buy" | "sell"


def make_levels(lower: Decimal, upper: Decimal, n: int) -> list[Decimal]:
    step = (upper - lower) / (n - 1)
    return [lower + step * i for i in range(n)]


def initial_slots(levels: list[Decimal], price: Decimal) -> list[Slot]:
    """Начальная раскладка: пропускаем уровень, ближайший к текущей цене."""
    nearest = min(range(len(levels)), key=lambda i: abs(levels[i] - price))
    slots = []
    for i, lvl in enumerate(levels):
        if i == nearest:
            continue
        slots.append(Slot(i, "buy" if lvl < price else "sell"))
    # сначала ближние к цене — они важнее, если rate limit притормозит размещение
    slots.sort(key=lambda s: abs(levels[s.level] - price))
    return slots


def counter_slot(filled: Slot, n_levels: int) -> Slot | None:
    """Какой ордер поставить после исполнения `filled`."""
    if filled.side == "buy":
        nxt = filled.level + 1
        return Slot(nxt, "sell") if nxt < n_levels else None
    nxt = filled.level - 1
    return Slot(nxt, "buy") if nxt >= 0 else None
