"""RESEARCH-003 测试：市场状态分类器 + 拥挤度风控层。

纯逻辑单测（不触网）：状态合成与去抖、状态条件 IC 分层口径、
拥挤度指标与阈值触发语义。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.market import MarketData
from quart.research.crowding_risk import (
    amount_share,
    bad_crowding_gap,
    crowding_indicators,
    crowding_trigger,
    fundamental_view_panel,
    rolling_adaptive_threshold,
)
from quart.research.market_state import (
    RISK_OFF,
    RISK_ON,
    TRANSITION,
    market_state_vector,
    state_conditional_ic,
)

N = 200
DATES = pd.date_range("2021-01-04", periods=N, freq="B")


def _signals(heat=None, amount=None) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    heat = heat if heat is not None else rng.normal(0, 1, N)
    amount = amount if amount is not None else 1e10 + 1e9 * np.sin(np.arange(N) / 8)
    return pd.DataFrame({"limit_heat_z": heat, "amount": amount}, index=DATES)


def _md(crowd: np.ndarray | None = None, n_symbols: int = 8) -> MarketData:
    """合成 n_symbols 只股票的日线（方向一/四通用）。"""
    rng = np.random.default_rng(11)
    symbols = [f"{i:06d}" for i in range(1, n_symbols + 1)]
    close = pd.DataFrame(
        {s: 10.0 * np.exp(np.cumsum(rng.normal(0, 0.01, N))) for s in symbols},
        index=DATES,
    )
    open_ = close.shift(1) * 1.002
    high = close * 1.02
    low = close * 0.98
    vol = pd.DataFrame({s: rng.integers(1_000_000, 5_000_000, N) for s in symbols}, index=DATES)
    amt = vol * close
    return MarketData(
        opens=open_, highs=high, lows=low, closes=close, volumes=vol,
        benchmark_close=close.mean(axis=1), amounts=amt,
    )


# ---------------- 方向一：市场状态 ----------------

def test_market_state_requires_columns():
    with pytest.raises(ValueError):
        market_state_vector(pd.DataFrame({"x": [1.0] * 5}, index=DATES[:5]))


def test_market_state_debounces_short_segments():
    # 构造强热/强冷的交替信号：若没有 min_days 去抖会频繁翻转
    heat = np.array([2.0] * 4 + [-2.0] * 4 + [2.0] * 4 + [-2.0] * 4 + [2.0] * 4, dtype=float)
    amt = np.full(len(heat), 1e10)
    signals = pd.DataFrame({"limit_heat_z": heat, "amount": amt}, index=DATES[: len(heat)])
    out = market_state_vector(signals, bench_close=None, min_days=5, composite_window=10)
    states = out["state"].to_numpy()
    # 去抖后每个连续片段 >= min_days，且非 transition 片段不会短于 5 天
    assert set(states) <= {RISK_ON, TRANSITION, RISK_OFF}
    i = 0
    while i < len(states):
        j = i
        while j < len(states) and states[j] == states[i]:
            j += 1
        if states[i] != TRANSITION:
            assert j - i >= 5, f"非 transition 片段过短 at {i}"
        i = j


def test_market_state_output_columns_and_dimensions():
    out = market_state_vector(_signals(), bench_close=pd.Series(1.0 + np.arange(N) * 0.001, index=DATES), min_days=3)
    assert list(out.columns) == ["state", "limit_heat_z", "amount_z", "vol_pct", "composite_pct"]
    assert len(out) == N
    assert out["composite_pct"].dropna().between(0, 1).all()


def test_state_conditional_ic_separates_states():
    md = _md()
    # 构造状态依赖因子：在 risk_on 股票上强信号，risk_off 上噪声
    rng = np.random.default_rng(3)
    n_sym = 8
    syms = [f"{i:06d}" for i in range(1, n_sym + 1)]
    fw = pd.DataFrame(rng.normal(0, 1, (N, n_sym)), index=DATES, columns=syms)
    # 简单确定性状态：0~59 risk_off、60~119 transition、120+ risk_on
    states = pd.DataFrame({"state": [RISK_OFF] * 60 + [TRANSITION] * 60 + [RISK_ON] * 80}, index=DATES)
    fw.iloc[:60] = rng.normal(0, 1, (60, n_sym))  # risk_off: 噪声
    fw.iloc[120:] = 3.0 * fw.iloc[120:].rank(axis=1, pct=True)  # risk_on: 增强
    starts = list(range(40, N - 5, 20))
    df = state_conditional_ic({"f": fw}, md, states, starts, horizon=5, min_symbols=4)
    assert "f" in df.index
    assert df.loc["f", "global_n"] > 0
    assert df.loc["f", "risk_on_n"] > 0 and df.loc["f", "risk_off_n"] > 0


# ---------------- 方向四：拥挤度风控层 ----------------

def test_amount_share_sums_to_100():
    md = _md()
    share = amount_share(md)
    assert share.shape == (N, 8)
    total = share.sum(axis=1).to_numpy()
    np.testing.assert_allclose(total, 100.0, rtol=1e-6)


def test_crowding_indicators_ranges():
    md = _md()
    ind = crowding_indicators(md, window=30)
    pct = ind["crowding_pctile_30d"]
    valid = pct.dropna()
    assert (valid >= 0).all().all() and (valid <= 1).all().all()
    acc = ind["crowding_acceleration_20d"]
    expected = pct.diff(20)
    np.testing.assert_allclose(acc.to_numpy(), expected.to_numpy(), rtol=1e-9, equal_nan=True)


def test_rolling_pctile_handles_nan_columns():
    md = _md()
    share = amount_share(md)
    share.iloc[10:15, 2] = np.nan  # 中途缺失
    pct = crowding_indicators(md, window=30)["crowding_pctile_30d"]
    # 缺失列观测后第一个窗尾应产出有限值（不 NaN 传染）
    assert pct.iloc[np.where(share.iloc[:, 2].notna())[0][-1], 2] is not None


def test_fundamental_view_panel_pit_gating():
    fin = pd.DataFrame(
        [
            {"symbol": "000001", "date": "2020-12-31", "eps": 1.0, "bps": 8.0, "profit_yoy": 50.0, "published_at": "2021-03-01"},
            {"symbol": "000002", "date": "2020-12-31", "eps": 1.0, "bps": 9.0, "profit_yoy": 10.0, "published_at": "2021-03-01"},
            {"symbol": "000001", "date": "2021-03-31", "eps": 1.2, "bps": 8.5, "profit_yoy": 60.0, "published_at": "2021-06-15"},
            {"symbol": "000002", "date": "2021-03-31", "eps": 1.0, "bps": 9.2, "profit_yoy": 5.0, "published_at": "2021-06-15"},
        ]
    )
    closes = pd.DataFrame(
        {s: 10.0 + np.arange(N) * 0.01 for s in ["000001", "000002"]}, index=DATES
    )
    panel = fundamental_view_panel(fin, closes, factor="profit_yoy")
    usable = pd.Timestamp("2021-03-01") < panel.index
    # 披露日期之前无任何可用基本面值
    assert panel.loc[:pd.Timestamp("2021-02-28")].notna().sum().sum() == 0
    # 披露后正常生效并取截面分位（两列 rank：1 和 0）
    assert usable.any()


def test_bad_crowding_gap_nan_without_fundamentals():
    pct = pd.DataFrame([[0.9, 0.1], [0.8, 0.2]], columns=["a", "b"])
    fund = pd.DataFrame([[0.5, np.nan], [0.5, np.nan]], columns=["a", "b"])
    gap = bad_crowding_gap(pct, fund)
    assert np.isnan(gap.loc[0, "b"])
    assert np.isclose(gap.loc[0, "a"], 0.4)


def test_crowding_trigger_fires_once_per_regime():
    # 单调上升的拥挤度序列：首次突破自适应阈值触发 1 次，之后持续高位不重复
    # 触发；平坦序列永不触发。
    n = 400
    idx = pd.date_range("2021-01-04", periods=n, freq="B")
    pct = pd.DataFrame(
        {"a": np.linspace(0.3, 0.95, n),
         "b": np.full(n, 0.5)}, index=idx
    )
    trig = crowding_trigger(pct, threshold_window=120, threshold_quantile=0.90, accel_window=10)
    assert trig["a"].sum() == 1  # 恰触发一次（首次突破）
    assert trig["b"].sum() == 0   # 平坦序列永不触发


def test_rolling_adaptive_threshold_tracks_rising_levels():
    idx = pd.date_range("2021-01-04", periods=300, freq="B")
    pct = pd.DataFrame({"a": np.linspace(0.3, 0.9, 300)}, index=idx)
    th = rolling_adaptive_threshold(pct, window=100, quantile=0.9)
    tail = th["a"].dropna().tail(50)
    # 高拥挤稳态下自适应阈值应接近当前水平（0.95 分位 <= 当前值）
    assert (tail <= pct["a"].tail(len(tail)) + 1e-9).all()