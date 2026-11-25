"""Логика грида (не зависит от биржи — легко тестировать).

Классический long-грид:
  * диапазон [lower, upper] делится на N уровней;
  * у каждой "ячейки" i (между level[i] и level[i+1]) может быть максимум 1 лот;
  * цена опустилась до level[i] и ячейка пустая  -> BUY  order_qty, ячейка занята;
  * цена поднялась до level[i+1] и ячейка занята -> SELL order_qty, ячейка свободна.
Каждая закрытая ячейка = профит примерно (шаг сетки * qty).
Максимальная позиция = (N-1) * order_qty.
"""
import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("grid")


@dataclass
class Action:
    side: str  # "buy" | "sell"
    cell: int
    level_price: Decimal


class Grid:
    def __init__(self, lower: Decimal, upper: Decimal, levels: int, state_file: str | None = None):
        step = (upper - lower) / (levels - 1)
        self.levels = [lower + step * i for i in range(levels)]
        self.step = step
        self.state_file = state_file
        self.filled: set[int] = set()  # индексы ячеек, где держим лот
        self._load()

    # --- state persistence (чтобы рестарт контейнера не ломал сетку) ---
    def _load(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            if [str(x) for x in self.levels] != data.get("levels"):
                log.warning("Параметры сетки изменились — старое состояние %s игнорируется", self.state_file)
                return
            self.filled = set(data.get("filled", []))
            log.info("Состояние загружено: занятые ячейки %s", sorted(self.filled))
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось прочитать состояние: %s", e)

    def save(self):
        if not self.state_file:
            return
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"levels": [str(x) for x in self.levels], "filled": sorted(self.filled)}, f)
        os.replace(tmp, self.state_file)

    # --- core ---
    def actions_for_price(self, price: Decimal) -> list[Action]:
        """Что нужно сделать при текущей цене. Состояние не меняет — вызови mark_done()."""
        acts: list[Action] = []
        n_cells = len(self.levels) - 1
        for i in range(n_cells):
            buy_px, sell_px = self.levels[i], self.levels[i + 1]
            if i in self.filled:
                if price >= sell_px:
                    acts.append(Action("sell", i, sell_px))
            else:
                # покупаем только если цена в ячейке или ниже, но не ниже нижней границы диапазона
                if price <= buy_px and price >= self.levels[0] - self.step:
                    acts.append(Action("buy", i, buy_px))
        # сначала продажи (снижают риск), покупки — от верхних ячеек к нижним
        acts.sort(key=lambda a: (a.side != "sell", -a.cell if a.side == "buy" else a.cell))
        return acts

    def mark_done(self, act: Action):
        if act.side == "buy":
            self.filled.add(act.cell)
        else:
            self.filled.discard(act.cell)
        self.save()

    def describe(self) -> str:
        return "levels=" + ", ".join(f"{x:.4f}" for x in self.levels)
