import asyncio

from gridbot.exchange.ratelimit import WeightLimiter


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    async def sleep(self, s): self.t += s


def test_never_exceeds_budget_and_keeps_reserve():
    clk = Clock()
    lim = WeightLimiter(limit=60, window=60, reserve=8, clock=clk, sleep=clk.sleep)

    async def go():
        for _ in range(52):
            await lim.acquire(1)
        assert clk.t == 1000.0  # 52 подряд без ожидания
        await lim.acquire(1)    # 53-й обычный — ждёт окно
        assert clk.t >= 1060.0
        await lim.acquire(1, critical=True)  # критичный может тратить резерв
    asyncio.run(go())


def test_sync_with_server_headers_is_conservative():
    clk = Clock()
    lim = WeightLimiter(limit=60, reserve=0, clock=clk, sleep=clk.sleep)
    lim.sync(60, 10, None)  # сервер говорит: осталось 10
    assert lim.used() == 50
    lim.sync(60, 40, None)  # больше, чем мы думаем — не уменьшаем учёт
    assert lim.used() == 50


def test_block():
    clk = Clock()
    lim = WeightLimiter(clock=clk, sleep=clk.sleep)
    lim.block(15)
    asyncio.run(lim.acquire(1))
    assert clk.t >= 1015
