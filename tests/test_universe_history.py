"""PIT 成分股测试。

用今天的成分股跑 2020 年的回测 = 前视偏差（A 股实测量级 3-8pp/yr），
比本项目已修复的退市股偏差（-2.0~-2.6pp/yr）更大。这里验证 PIT 查询
确实按日期返回不同集合，而不是永远返回当前快照。
"""
from __future__ import annotations

import pandas as pd
import pytest

from quart.data.universe_history import (
    build_history_from_snapshots,
    constituents_at,
    describe,
    load_history,
    save_history,
)


@pytest.fixture
def hist(tmp_path, monkeypatch):
    """构造一个小规模 PIT 历史并写盘。"""
    changes = pd.DataFrame([
        {"symbol": "600000", "in_date": "2019-01-01", "out_date": "2024-01-01"},
        {"symbol": "600519", "in_date": "2019-01-01", "out_date": pd.NaT},
        {"symbol": "000001", "in_date": "2024-01-01", "out_date": pd.NaT},
    ])
    monkeypatch.setattr("quart.data.universe_history.data_root", lambda: tmp_path)
    return save_history("TEST", changes)


def test_constituents_vary_by_date(hist):
    """同一指数在不同日期返回不同成分股——PIT 的核心不变量。"""
    assert constituents_at("TEST", "2020-06-01") == ["600000", "600519"]
    assert constituents_at("TEST", "2025-06-01") == ["000001", "600519"]
    # 600000 在 2024-01-01 调出，之后不应出现
    assert "600000" not in (constituents_at("TEST", "2024-06-01") or [])


def test_unknown_index_returns_none(hist):
    assert constituents_at("NOPE", "2020-01-01") is None


def test_get_constituents_uses_pit_when_as_of_given(hist, monkeypatch):
    from quart.data import universe

    # history_path = data_root()/"universe"/f"{index}_constituents_history.parquet"
    # hist 位于 tmp_path/universe/ 下，故 data_root 应为 tmp_path（hist.parent.parent）
    monkeypatch.setattr("quart.data.universe_history.data_root", lambda: hist.parent.parent)
    monkeypatch.setattr(universe, "_cache_path", lambda code: hist)

    # as_of 在历史覆盖范围内 → 走 PIT，不得回退快照
    got = universe.get_constituents("TEST", as_of="2020-06-01")
    assert got == ["600000", "600519"]


def test_get_constituents_falls_back_with_warning(hist, monkeypatch):
    """无 PIT 记录时必须留下可见告警，不能静默降级。"""
    from loguru import logger

    from quart.data import universe

    monkeypatch.setattr("quart.data.universe_history.data_root", lambda: hist.parent.parent)
    monkeypatch.setattr(universe, "_cache_path", lambda code: hist)

    warnings: list[str] = []
    logger.remove()
    logger.add(lambda m: warnings.append(m), level="WARNING")

    # MISSING 指数没有历史 → 回退快照（快照被 patch 成 hist 文件）
    universe.get_constituents("MISSING", as_of="2020-06-01")
    assert any("前视偏差" in w for w in warnings), "回退到当前快照时未告警"


def test_get_constituents_refuses_silent_fallback_when_strict(hist):
    from quart.data import universe

    out_of_range = "2030-01-01"  # 历史里没有"仍在池内"的记录覆盖到这天之外其实有
    # 历史末端仍在池内的股票会一直命中；这里改用不存在的指数验证严格模式
    with pytest.raises(RuntimeError, match="PIT"):
        universe.get_constituents("MISSING", as_of=out_of_range, strict_pit=True)


def test_build_history_from_snapshots():
    snaps = {
        "2024-01-01": ["600000", "600519"],
        "2024-07-01": ["600519", "000001"],
    }
    hist = build_history_from_snapshots("X", snaps)
    # 600000 调出、000001 调入，600519 一直在
    out_dates = hist.set_index("symbol")["out_date"]
    assert pd.isna(out_dates["600519"])
    assert out_dates["600000"] == pd.Timestamp("2024-07-01")
    assert hist.set_index("symbol").loc["000001", "in_date"] == pd.Timestamp("2024-07-01")


def test_describe_reports_missing_history(tmp_path, monkeypatch):
    monkeypatch.setattr("quart.data.universe_history.data_root", lambda: tmp_path)
    msg = describe("000300")
    assert "前视偏差" in msg
    assert load_history("000300") is None


def test_filter_for_pit_universe_applies_membership_per_date(monkeypatch):
    from quart.data import universe

    dates = pd.date_range("2020-01-01", periods=3, freq="D")
    bars = pd.DataFrame({
        "date": dates.repeat(2), "symbol": ["1", "2"] * 3,
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0,
    })
    hist = pd.DataFrame({
        "symbol": ["000001", "000002"],
        "in_date": [dates[0], dates[1]], "out_date": [dates[0], dates[2]],
    })
    monkeypatch.setattr("quart.data.universe_history.load_history", lambda index: hist)
    out = universe.filter_for_pit_universe(bars, "TEST")
    assert list(out[["date", "symbol"]].itertuples(index=False, name=None)) == [
        (dates[0], "1"), (dates[1], "2"), (dates[2], "2"),
    ]


def test_filter_for_pit_universe_blocks_missing_coverage(monkeypatch):
    from quart.data import universe

    dates = pd.date_range("2020-01-01", periods=2, freq="D")
    bars = pd.DataFrame({
        "date": dates, "symbol": ["1", "1"], "open": 1.0,
        "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
    })
    hist = pd.DataFrame({"symbol": ["000001"], "in_date": [dates[0]], "out_date": [dates[0]]})
    monkeypatch.setattr("quart.data.universe_history.load_history", lambda index: hist)
    with pytest.raises(RuntimeError, match="未覆盖"):
        universe.filter_for_pit_universe(bars, "TEST")
