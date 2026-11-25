"""Watchdog: если event loop завис — убиваем процесс, docker перезапустит контейнер.

Отдельный поток (не asyncio), поэтому срабатывает даже при зависшем loop.
Также пишет heartbeat-файл для HEALTHCHECK в Dockerfile.
"""
import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("watchdog")


class Watchdog(threading.Thread):
    def __init__(self, last_beat: Callable[[], float], timeout: float, heartbeat_file: str | None):
        super().__init__(daemon=True, name="watchdog")
        self.last_beat = last_beat
        self.timeout = timeout
        self.heartbeat_file = heartbeat_file

    def run(self):
        while True:
            time.sleep(5)
            age = time.monotonic() - self.last_beat()
            if self.heartbeat_file and age < self.timeout:
                try:
                    with open(self.heartbeat_file, "w") as f:
                        f.write(str(int(time.time())))
                except OSError:
                    pass
            if age > self.timeout:
                log.critical("Бот не подаёт признаков жизни %.0fs — аварийный выход для перезапуска", age)
                logging.shutdown()
                os._exit(3)
