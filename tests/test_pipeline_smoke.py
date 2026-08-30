"""实盘信号流水线端到端冒烟测试。

覆盖此前**完全无测试**的 `quart/pipeline.py`——它是唯一会真正生成
"人照着下单"的委托计划的路径，却连一个断言都没有。

不依赖真实数据/网络：用合成 MarketData + 临时 holdings.json。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quart.execution import FLAT
from quart.pipeline import generate_orders, load_holdings, render_report


def test_load_holdings_missing_file_returns_empty(tmp_path):
    cash, positions = load_holdings(tmp_path / "nope.json")
    assert cash == 0.0
    assert positions == {}


def test_load_holdings_normalizes_types(tmp_path):
    p = tmp_path / "holdings.json"
    p.write_text(json.dumps({"cash": 50000, "positions": {"600519": "200", "601318": 800}}),
                 encoding="utf-8")
    cash, positions = load_holdings(p)
    assert cash == 50000.0
    assert positions == {"600519": 200, "601318": 800}
    assert all(isinstance(v, int) for v in positions.values())


def test_generate_orders_buys_target_portfolio():
    prices = pd.Series({"A": 10.0, "B": 10.0})
    orders, equity = generate_orders(
        {"A": 0.5, "B": 0.5}, prices, cash=100_000.0, positions={}
    )
    assert equity == 100_000.0
    # 整手 1000 元/手（10 元 × 100 股）→ 单票落 49 手 = 49000 元，
    # 剩 2000 元不够覆盖费用垫 + 再买一手，故总利用率约 98%
    total = sum(o.shares * o.ref_price for o in orders)
    assert total <= 100_000.0
    assert total > 100_000.0 * 0.95, f"资金利用率过低: {total}"
    assert all(o.side == "BUY" for o in orders)
    assert {o.symbol for o in orders} == {"A", "B"}


def test_generate_orders_force_flat_sells_everything():
    prices = pd.Series({"A": 10.0, "B": 20.0})
    orders, _ = generate_orders(
        {FLAT: 1.0}, prices, cash=1000.0, positions={"A": 500, "B": 100}, force_flat=True
    )
    assert {o.symbol: o.shares for o in orders} == {"A": 500, "B": 100}
    assert all(o.side == "SELL" for o in orders)


def test_generate_orders_is_affordable_after_fees():
    """委托计划必须买得起——含费用在内不能超出可用资金。"""
    prices = pd.Series({"A": 10.0})
    orders, _ = generate_orders({"A": 1.0}, prices, cash=100_000.0, positions={})
    cost = sum(o.amount + o.fee for o in orders)
    assert cost <= 100_000.0, f"计划超出可用资金: {cost} > 100000"


def test_generate_orders_reports_shortage_as_warning():
    prices = pd.Series({"A": 50.0})
    warnings: list[str] = []
    orders, _ = generate_orders(
        {"A": 1.0}, prices, cash=100.0, positions={}, warnings=warnings
    )
    # 100 元买不起 50 元/股的一手（5000 元）
    assert orders == []
    assert any("不足" in w for w in warnings), "资金不足时必须给出提示，不能静默跳过"


def test_generate_orders_rebalances_without_forcing_full_liquidation():
    """目标权重未变时不应产生交易（此前实盘路径缺少 min_order_value 死区）。"""
    prices = pd.Series({"A": 10.0})
    orders, _ = generate_orders(
        {"A": 0.5}, prices, cash=50_000.0, positions={"A": 5000}  # 已持 5 万 = 50%
    )
    assert orders == [], "权重已达标却仍产生委托（换手噪声）"


def test_limit_down_produces_warning_not_silence():
    """昨收跌停：实盘只提示不拒单（次日可能开板），但必须让人看见。"""
    prev = pd.Series({"A": 10.0})
    last = pd.Series({"A": 9.0})   # 前收 10 → 跌停 9.0，今收正是跌停价
    warnings: list[str] = []
    orders, _ = generate_orders(
        {}, last, cash=0.0, positions={"A": 1000},
        warnings=warnings, prev_close=prev,
    )
    assert any("跌停" in w for w in warnings)
    # 提示而非拒单：委托仍要生成，能否成交交给市场
    assert [o.symbol for o in orders] == ["A"]


def test_limit_detection_requires_prev_close_not_same_day_close():
    """回归：prev_close 误传当日收盘会让涨跌停检测静默失效。"""
    last = pd.Series({"A": 9.0})
    warnings: list[str] = []
    generate_orders({}, last, cash=0.0, positions={"A": 1000}, warnings=warnings)
    # 未传 prev_close 时退化为当日收盘，不应误报跌停（宁可不报也不误报）
    assert not any("跌停" in w for w in warnings)


def test_render_report_includes_warnings_and_disclaimer():
    prices = pd.Series({"A": 10.0})
    orders, equity = generate_orders({"A": 0.5}, prices, cash=100_000.0, positions={})
    report = render_report(
        pd.Timestamp("2024-01-02"), "lowvol_indz", orders, equity, ["测试告警"]
    )
    assert "Quart 每日信号" in report
    assert "测试告警" in report
    assert "不构成投资建议" in report, "报告必须带免责声明"


def test_render_report_handles_empty_orders():
    report = render_report(
        pd.Timestamp("2024-01-02"), "lowvol_indz", [], 100_000.0, []
    )
    assert "今日无调仓信号" in report


def test_render_report_includes_manual_plan_identity():
    report = render_report(
        pd.Timestamp("2026-08-28"),
        "lowvol_indz",
        [],
        100_000.0,
        [],
        plan_id="plan_20260828_abcdef12",
        intended_trade_date="2026-08-31",
    )
    assert "plan_20260828_abcdef12" in report
    assert "2026-08-31" in report
    assert "DRAFT" in report


@pytest.mark.parametrize("cash", [0.0, 1.0, 999.0])
def test_tiny_cash_never_overflows(cash):
    prices = pd.Series({"A": 10.0, "B": 20.0})
    orders, equity = generate_orders(
        {"A": 0.5, "B": 0.5}, prices, cash=cash, positions={}
    )
    assert orders == []
    assert equity >= 0.0
