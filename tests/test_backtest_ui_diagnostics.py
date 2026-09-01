"""回测中心展示诊断：基准对比、风险补算、代码格式与主题 helper。"""
from __future__ import annotations

import json

import pandas as pd
import pytest


@pytest.fixture
def backtest_files(tmp_path, monkeypatch):
    import api.backtest_api as api

    dates = pd.date_range("2026-01-05", periods=8, freq="B")
    pd.DataFrame({
        "date": dates,
        "equity": [100, 103, 101, 106, 104, 108, 107, 112],
    }).to_csv(tmp_path / "equity_demo.csv", index=False)
    (tmp_path / "summary_demo.json").write_text(
        json.dumps({
            "start": "2026-01-05",
            "end": "2026-01-14",
            "initial_cash": 100,
            "total_return": 0.12,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(
        api,
        "_BENCH_CACHE",
        pd.Series([100, 101, 100, 102, 101, 104, 105, 106], index=dates, dtype=float),
    )
    return api, tmp_path


def test_benchmark_comparison_normalizes_and_builds_excess(backtest_files):
    api, _ = backtest_files
    frame = api.get_benchmark_comparison("demo")
    assert frame is not None
    assert frame.iloc[0]["strategy_nav"] == pytest.approx(1.0)
    assert frame.iloc[0]["benchmark_nav"] == pytest.approx(1.0)
    assert frame.iloc[-1]["strategy_nav"] == pytest.approx(1.12)
    assert frame.iloc[-1]["benchmark_nav"] == pytest.approx(1.06)
    assert frame.iloc[-1]["excess_nav"] == pytest.approx(1.12 / 1.06)


def test_performance_diagnostics_contains_relative_tail_and_drawdown(backtest_files):
    api, _ = backtest_files
    result = api.get_performance_diagnostics("demo")
    assert result is not None
    assert result["sortino"] is not None
    assert result["information_ratio"] is not None
    assert result["tracking_error"] > 0
    assert result["worst_day"] < 0
    assert result["cvar_95"] <= result["worst_day"] + 1e-12
    assert result["max_drawdown_duration"] >= 1
    assert result["drawdown_peak_date"] <= result["drawdown_trough_date"]


def test_trade_codes_remain_six_digits(backtest_files, monkeypatch):
    api, root = backtest_files
    pd.DataFrame({
        "date": ["2026-01-05"],
        "symbol": [155],
        "side": ["BUY"],
        "shares": [100],
        "price": [10.0],
        "amount": [1000.0],
        "fee": [5.0],
    }).to_csv(root / "trades_demo.csv", index=False)
    monkeypatch.setattr("common.load_stock_names", lambda: {"000155": "川能动力"})

    frame = api.get_trades("demo")
    assert frame is not None
    assert frame.iloc[0]["代码"] == "000155"
    assert frame.iloc[0]["名称"] == "川能动力"


def test_theme_helpers_escape_values_and_build_grid():
    from frontend.theme import metric_card, metric_grid, page_header

    card = metric_card("收益<script>", "<b>1%</b>", "green")
    assert "<script>" not in card
    assert "&lt;script&gt;" in card
    assert "<b>1%</b>" not in card
    assert "metric-green" in card
    assert metric_grid([card]).startswith('<div class="metric-grid">')
    assert "page-eyebrow" in page_header("回测", "说明", "AUDIT")
