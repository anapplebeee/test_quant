"""换手缓冲带（rank-buffer hysteresis）单元测试。"""
from quart.strategy.lowvol_composite import LowVolCompositeStrategy

sel = LowVolCompositeStrategy._buffer_select


def test_buffer_zero_equals_fresh_topk():
    ranked = list("ABCDEFGH")
    assert sel(ranked, set(), 3, 0.0) == list("ABC")
    # buffer=0 时集合与纯 top_k 一致（等权组合与顺序无关）
    assert set(sel(ranked, {"B", "F"}, 3, 0.0)) == {"A", "B", "C"}


def test_buffer_keeps_holding_within_band():
    ranked = list("ABCDEFGH")
    held = {"C", "D", "E"}  # 排名 3/4/5，top_k=3 buffer=0.5 -> 保留区前 4 名
    assert sel(ranked, held, 3, 0.5) == ["C", "D", "A"]  # E 出区，补入最优新名 A


def test_buffer_drops_when_out_of_band():
    ranked = list("ABCDEFGH")
    held = {"F"}  # 排名 6 > 4
    assert sel(ranked, held, 3, 0.5) == ["A", "B", "C"]


def test_buffer_zero_turnover_when_all_in_band():
    ranked = list("ABCDEFGH")
    held = {"B", "C", "D"}  # 全部在保留区
    assert sel(ranked, held, 3, 0.5) == ["B", "C", "D"]


def test_buffer_large_band():
    ranked = list("ABCDEFGH")
    held = {"E", "F"}  # 排名 5/6，top_k=3 buffer=1.0 -> 保留区前 6 名
    assert sel(ranked, held, 3, 1.0) == ["E", "F", "A"]


def test_buffer_never_exceeds_topk():
    ranked = list("ABCDE")
    held = {"A", "B", "C", "D", "E"}
    assert len(sel(ranked, held, 3, 1.0)) == 3
