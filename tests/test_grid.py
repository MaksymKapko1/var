from decimal import Decimal as D

from gridbot.grid import Grid


def test_levels():
    g = Grid(D(100), D(200), 11)
    assert g.levels[0] == 100 and g.levels[-1] == 200 and g.step == 10


def test_buy_then_sell_cycle(tmp_path):
    f = str(tmp_path / "s.json")
    g = Grid(D(100), D(200), 11, f)
    # цена 205 выше диапазона — ничего
    assert g.actions_for_price(D(205)) == []
    # цена 189 -> покупаем ячейку 9 (190-200)
    acts = g.actions_for_price(D(189))
    assert [(a.side, a.cell) for a in acts] == [("buy", 9)]
    g.mark_done(acts[0])
    # повторно не покупаем
    assert g.actions_for_price(D(189)) == []
    # цена 200 -> продаём
    acts = g.actions_for_price(D(200))
    assert [(a.side, a.cell) for a in acts] == [("sell", 9)]
    g.mark_done(acts[0])
    assert g.filled == set()


def test_gap_down_buys_multiple_and_state_persists(tmp_path):
    f = str(tmp_path / "s.json")
    g = Grid(D(100), D(200), 11, f)
    for a in g.actions_for_price(D(165)):
        g.mark_done(a)
    assert g.filled == {7, 8, 9}
    g2 = Grid(D(100), D(200), 11, f)
    assert g2.filled == {7, 8, 9}
    # изменили параметры — состояние игнорируется
    assert Grid(D(100), D(300), 11, f).filled == set()


def test_far_below_range_no_buys():
    g = Grid(D(100), D(200), 11)
    assert g.actions_for_price(D(50)) == []
