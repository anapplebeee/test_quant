"""window_stats / summarize 区间窗口指标回归（近半年 126、近1年 252 交易日口径）。"""

import numpy as np
import pandas as pd
import pytest

from quart.backtest.metrics import WINDOWS, summarize, window_stats


def _equity(n: int, daily_ret: float, start="2020-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100.0 * (1 + daily_ret) ** np.arange(n), index=idx)


def test_window_stats_rising_series():
    eq = _equity(400, 0.001)  # 日涨 0.1%
    w1y = window_stats(eq, 252)
    assert w1y["return"] == pytest.approx(1.001**252 - 1, rel=1e-6)
    assert w1y["mdd"] == 0.0  # 单调上升无回撤
    assert w1y["sharpe"] > 0
    w6m = window_stats(eq, 126)
    assert w6m["return"] == pytest.approx(1.001**126 - 1, rel=1e-6)


def test_window_stats_drawdown_window():
    # 前 300 天平稳，最后 100 天腰斩：近1年应捕捉到回撤，近半年更大
    n = 400
    idx = pd.bdate_range("2020-01-01", periods=n)
    vals = np.concatenate([np.full(300, 100.0), np.linspace(100.0, 50.0, 100)])
    eq = pd.Series(vals, index=idx)
    w1y = window_stats(eq, 252)
    w6m = window_stats(eq, 126)
    assert w1y["mdd"] == pytest.approx(-0.5, rel=1e-6)
    assert w6m["mdd"] == pytest.approx(-0.5, rel=1e-6)
    assert w1y["return"] < 0


def test_window_stats_insufficient_sample():
    eq = _equity(50, 0.001)
    w = window_stats(eq, 252)
    # 样本不足时仍返回可得区间（50 天），不为 None 崩溃
    assert w["days"] == 49
    assert w["return"] is not None
    empty = window_stats(pd.Series(dtype=float), 252)
    assert empty["return"] is None and empty["days"] == 0


def test_summarize_contains_window_keys():
    eq = _equity(400, 0.001)
    bench = _equity(400, 0.0003)
    s = summarize(eq, benchmark=bench)
    for label, _ in WINDOWS:
        assert f"{label}_return" in s
        assert f"{label}_mdd" in s
        assert f"bench_{label}_return" in s
        assert f"bench_{label}_mdd" in s
    # 全周期键不受影响
    assert "total_return" in s and "cagr" in s
