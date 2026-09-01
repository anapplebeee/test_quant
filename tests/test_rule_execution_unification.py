"""RULE-002：回测与信号执行必须使用同一份 RuleBook 解析结果。"""
from __future__ import annotations

import pandas as pd

from quart.data.calendar import TradingCalendar
from quart.data.security_master import SecurityMaster
from quart.execution import (
    BUY,
    BacktestExecutionModel,
    ExecutionContext,
    ExecutionRuleResolver,
    Fees,
    LiveExecutionModel,
    generate_orders,
)
from quart.market_rules.rule_book import RuleBook, RuleSet, default_rule_book


def _master(*rows: dict) -> SecurityMaster:
    return SecurityMaster(pd.DataFrame(rows))


def _resolver(master: SecurityMaster, book: RuleBook | None = None) -> ExecutionRuleResolver:
    return ExecutionRuleResolver(
        rule_book=book or default_rule_book(),
        security_master=master,
        calendar=TradingCalendar(),
    )


def _buy_context(
    symbol: str,
    *,
    trade_date: str,
    price: float,
    prev_close: float = 10.0,
    resolver: ExecutionRuleResolver,
) -> ExecutionContext:
    return ExecutionContext(
        date=pd.Timestamp(trade_date),
        targets={symbol: 1.0},
        equity=100_000.0,
        cash=100_000.0,
        positions={},
        mark_prices=pd.Series({symbol: prev_close}),
        exec_prices=pd.Series({symbol: price}),
        prev_closes=pd.Series({symbol: prev_close}),
        fees=Fees.zero(),
        rule_resolver=resolver,
    )


def test_backtest_obeys_historical_chinext_limit_not_static_prefix():
    """2020-08-24 前创业板仍是 10%，改革日才切换 20%。"""
    master = _master({"symbol": "300001", "listed_at": "2010-01-01", "status": "listed"})
    resolver = _resolver(master)

    before = generate_orders(
        _buy_context("300001", trade_date="2020-08-21", price=11.5, resolver=resolver),
        BacktestExecutionModel(Fees.zero(), rule_resolver=resolver),
    )
    assert before.orders == []
    assert "RuleBook" in (before.skipped[0].blocked_reason or "")

    after = generate_orders(
        _buy_context("300001", trade_date="2020-08-24", price=11.5, resolver=resolver),
        BacktestExecutionModel(Fees.zero(), rule_resolver=resolver),
    )
    assert [(order.symbol, order.side) for order in after.orders] == [("300001", BUY)]


def test_security_master_st_and_lifecycle_statuses_block_orders():
    master = _master(
        {
            "symbol": "600001",
            "listed_at": "2010-01-01",
            "status": "listed",
            "status_effective_from": "2010-01-01",
        },
        {
            "symbol": "600001",
            "listed_at": "2010-01-01",
            "status": "st",
            "status_effective_from": "2024-01-01",
        },
        {
            "symbol": "600002",
            "listed_at": "2024-02-01",
            "status": "listed",
        },
        {
            "symbol": "600003",
            "listed_at": "2010-01-01",
            "delisted_at": "2024-01-01",
            "status": "listed",
        },
    )
    resolver = _resolver(master)
    model = BacktestExecutionModel(Fees.zero(), rule_resolver=resolver)

    st = generate_orders(
        _buy_context("600001", trade_date="2024-01-02", price=10.6, resolver=resolver), model
    )
    assert "RuleBook st" in (st.skipped[0].blocked_reason or "")

    pre_listing = generate_orders(
        _buy_context("600002", trade_date="2024-01-02", price=10.0, resolver=resolver), model
    )
    assert "尚未上市" in (pre_listing.skipped[0].blocked_reason or "")

    delisted = generate_orders(
        _buy_context("600003", trade_date="2024-01-02", price=10.0, resolver=resolver), model
    )
    assert "已退市" in (delisted.skipped[0].blocked_reason or "")


def test_rulebook_lot_size_controls_shared_order_generator():
    """同一 RuleBook 的 lot_size 既作用回测也作用信号委托。"""
    book = RuleBook([
        RuleSet("SSE", "MAIN", "stock", "listed", None, None, 0.10, lot_size=200),
    ])
    master = _master({"symbol": "600001", "listed_at": "2010-01-01", "status": "listed"})
    resolver = _resolver(master, book)
    ctx = _buy_context("600001", trade_date="2024-01-02", price=10.0, resolver=resolver)

    backtest = generate_orders(ctx, BacktestExecutionModel(Fees.zero(), rule_resolver=resolver))
    live = generate_orders(ctx, LiveExecutionModel(Fees.zero(), rule_resolver=resolver))
    assert backtest.orders[0].shares % 200 == 0
    assert live.orders[0].shares % 200 == 0


def test_live_model_warns_from_same_rulebook_without_silent_rejection():
    master = _master({"symbol": "600001", "listed_at": "2010-01-01", "status": "listed"})
    resolver = _resolver(master)
    ctx = _buy_context("600001", trade_date="2024-01-02", price=11.0, resolver=resolver)
    live = LiveExecutionModel(Fees.zero(), rule_resolver=resolver)

    plan = generate_orders(ctx, live)
    assert plan.orders
    assert any("RuleBook" in warning and "涨停" in warning for warning in live.warnings)
