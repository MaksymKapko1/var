"""Персистентное состояние бота (атомарная запись JSON на volume /data)."""
import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger("state")

VERSION = 2


@dataclass
class TrackedOrder:
    level: int
    side: str
    cid: str
    price: str
    size: str
    counter: bool = False   # встречный ордер (закрывает цикл buy->sell или sell->buy)


@dataclass
class PendingSlot:
    level: int
    side: str
    not_before: float = 0.0
    attempts: int = 0
    last_error: str = ""
    counter: bool = False


@dataclass
class BotState:
    params: dict = field(default_factory=dict)
    base_size: str | None = None
    orders: dict[str, TrackedOrder] = field(default_factory=dict)   # order_id -> TrackedOrder
    pending: list[PendingSlot] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"buys": 0, "sells": 0, "round_trips": 0, "grid_profit": "0",
                                                 "rejects": 0})
    mode: str = "running"      # running | paused (цена вне диапазона) | halted (стоп, нужен человек)
    halt_reason: str = ""
    seq: int = 0

    def busy_levels(self) -> set[int]:
        return {o.level for o in self.orders.values()}


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> BotState | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path) as f:
                d = json.load(f)
            if d.get("version") != VERSION:
                log.warning("Состояние старой версии (%s) — игнорирую", d.get("version"))
                return None
            return BotState(
                params=d["params"], base_size=d.get("base_size"),
                orders={k: TrackedOrder(**v) for k, v in d.get("orders", {}).items()},
                pending=[PendingSlot(**p) for p in d.get("pending", [])],
                stats=d.get("stats", {}), mode=d.get("mode", "running"),
                halt_reason=d.get("halt_reason", ""), seq=d.get("seq", 0),
            )
        except Exception as e:  # noqa: BLE001
            log.error("Состояние %s повреждено (%s) — игнорирую", self.path, e)
            return None

    def save(self, st: BotState):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        d = asdict(st)
        d["version"] = VERSION
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
