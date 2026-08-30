"""窗口统计的短数据边界测试。

回归背景：首页在回测样本 174 天、请求 252 日窗口时崩溃
（`IndexError: index -253 is out of bounds`）。根因是
`get_window_stats` 用 `eq.index[-(days+1)]` 取窗口起点——数据不足时越界；
而 `window_stats` 内部用 `iloc` 切片会自动 clamp 到 0，两者行为不一致。

任何"近 N 日"指标都必须能优雅处理"数据其实没那么长"。
"""
from __future__ import annotations

import pandas as pd
import pytest

from quart.backtest.metrics import TRADING_DAYS, WINDOWS, window_stats


def _equity(n: int) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.Series([1_000_000 * (1 + 0.0004) ** i for i in range(n)], index=idx, name="equity")


@pytest.mark.parametrize("n,days", [
    (10, 252),    # 远短于窗口
    (174, 252),   # 实际崩溃案例
    (100, 126),
    (2, 252),     # 极短
    (1, 252),     # 单点
])
def test_window_stats_never_raises_on_short_data(n, days):
    out = window_stats(_equity(n), days)
    assert set(out) >= {"return", "mdd", "ann_vol", "sharpe", "days"}
    # 不足 2 点无法算收益
    if n < 2:
        assert out["return"] is None
    assert out["days"] >= 0


def test_window_stats_days_is_clamped_not_negative():
    """窗口长于数据时，days 应等于可用长度-1，不能是负数。"""
    out = window_stats(_equity(30), 252)
    assert out["days"] == 29
    assert out["return"] is not None


def test_window_stats_full_window_uses_exact_length():
    out = window_stats(_equity(300), 126)
    assert out["days"] == 126, "数据充足时应取满窗口"


def test_all_configured_windows_safe_on_short_data():
    """WINDOWS 里的每个窗口都必须安全——首页会全部调用一遍。"""
    for n in (1, 2, 5, 30, 174):
        for _label, days in WINDOWS:
            out = window_stats(_equity(n), days)
            assert out["days"] >= 0


def test_window_stats_handles_nan_in_equity():
    eq = _equity(50)
    eq.iloc[10:20] = float("nan")
    out = window_stats(eq, 252)
    assert out["days"] >= 0
    # dropna 后仍应能算出收益
    assert out["return"] is not None


def test_window_stats_handles_zero_baseline():
    """净值为 0 会产生 inf——不能返回 inf，UI 会显示成 "+inf%"。"""
    eq = _equity(30)
    eq.iloc[0] = 0.0
    out = window_stats(eq, 126)
    assert out["days"] >= 0
    # 除零后收益不可定义，应为 None 而非 inf
    assert out["return"] is None or abs(out["return"]) == float("inf") is False
    for key in ("return", "mdd", "ann_vol", "sharpe"):
        v = out[key]
        assert v is None or abs(v) != float("inf"), f"{key} 返回了 inf: {v}"


def test_benchmark_window_uses_clamped_index(tmp_path, monkeypatch):
    """get_window_stats 的基准对齐必须与 window_stats 的 clamp 行为一致。

    这是崩溃的直接位置：`eq.index[-(days+1)]` 在短数据上越界。
    """
    from api import backtest_api

    eq = _equity(174)
    df = pd.DataFrame({"date": eq.index, "equity": eq.values})

    monkeypatch.setattr(backtest_api, "get_equity_curve", lambda name: df)

    bench = pd.Series(
        [3000.0 * (1 + 0.0002) ** i for i in range(174)],
        index=eq.index,
    )
    monkeypatch.setattr(backtest_api, "_benchmark_series", lambda: bench)

    out = backtest_api.get_window_stats("short")
    assert out is not None
    for _label, _days in WINDOWS:
        entry = out.get(_label)
        assert entry is not None
        # 短于窗口时窗口被 clamp，不应因为越界而缺项
        assert entry["days"] > 0


def test_benchmark_absent_does_not_break_windows(tmp_path, monkeypatch):
    """基准拉不到时窗口指标仍应产出（只是没有 bench_* 字段）。"""
    from api import backtest_api

    eq = _equity(174)
    monkeypatch.setattr(
        backtest_api, "get_equity_curve",
        lambda name: pd.DataFrame({"date": eq.index, "equity": eq.values}),
    )
    monkeypatch.setattr(backtest_api, "_benchmark_series",
                        lambda: pd.Series(dtype=float))

    out = backtest_api.get_window_stats("short")
    assert out is not None
    for label, _days in WINDOWS:
        assert out[label]["return"] is not None
        assert "bench_return" not in out[label]
