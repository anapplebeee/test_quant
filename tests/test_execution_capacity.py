"""EXEC-002：ADV 容量上限、部分成交与剩余意图回归。"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd

from quart.backtest.engine import BacktestEngine
from quart.data.market import MarketData
from quart.execution import BacktestExecutionModel, ExecutionContext, Fees, generate_orders
from quart.strategy.base import BaseStrategy


def _ctx(*, adv: float, participation: float = 0.1) -> ExecutionContext:
    prices = pd.Series({"600001": 10.0})
    return ExecutionContext(
        date=pd.Timestamp("2024-01-02"),
        targets={"600001": 1.0},
        equity=10_000.0,
        cash=10_000.0,
        positions={},
        mark_prices=prices,
        exec_prices=prices,
        prev_closes=prices,
        adv=pd.Series({"600001": adv}),
        max_adv_participation=participation,
        fees=Fees.zero(),
    )


def test_capacity_caps_buy_and_retains_deferred_intent():
    # ADV=10,000、参与率 10%、价格 10 元 → 最多 100 股。
    plan = generate_orders(_ctx(adv=10_000), BacktestExecutionModel(Fees.zero()))

    assert plan.orders[0].shares == 100
    assert plan.orders[0].deferred_shares == 900
    assert plan.orders[0].deferred_reason == "ADV容量约束"
    assert plan.has_capacity_deferral
    assert plan.orders[0].amount <= 10_000 * 0.1


def test_capacity_below_one_lot_defers_without_fake_fill():
    # 参与率可成交额不足一手，不能用碎股假设成交。
    plan = generate_orders(_ctx(adv=5_000), BacktestExecutionModel(Fees.zero()))

    assert plan.orders == []
    assert plan.has_capacity_deferral
    assert plan.skipped[0].deferred_shares == 1_000
    assert "ADV 容量不足一手" in (plan.skipped[0].blocked_reason or "")


def test_capacity_caps_sell_without_ignoring_exit_intent():
    ctx = replace(
        _ctx(adv=10_000),
        targets={},
        cash=0.0,
        equity=10_000.0,
        positions={"600001": 1_000},
    )

    plan = generate_orders(ctx, BacktestExecutionModel(Fees.zero()))

    assert plan.orders[0].side == "SELL"
    assert plan.orders[0].shares == 100
    assert plan.orders[0].deferred_shares == 900
    assert plan.ending_positions == {"600001": 900}


class _OnceTarget(BaseStrategy):
    name = "once_target"

    def prepare(self, md):
        self.sent = False

    def target_weights(self, i: int):
        if not self.sent:
            self.sent = True
            return {"600001": 1.0}
        return {}


def _market_data() -> MarketData:
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    return MarketData.from_bars(
        pd.DataFrame(
            {
                "date": dates,
                "symbol": "600001",
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1_000.0,
                "amount": 10_000.0,
            }
        )
    )


def test_backtest_carries_capacity_residual_to_following_trade_days():
    engine = BacktestEngine(
        _market_data(),
        _OnceTarget(),
        fees=Fees.zero(),
        initial_cash=10_000.0,
        max_adv_participation=0.1,
    )
    result = engine.run_result()
    buys = result.trades[result.trades["side"] == "BUY"]

    # 第一天信号后的每个执行日只成交 100 股，未成交部分不会因策略只发一次
    # 信号而丢失；样本结束时仍可在 result 中审计。
    assert buys["shares"].tolist() == [100, 100, 100, 100]
    assert not result.deferred_orders.empty
    assert result.pending_targets == {"600001": 1.0}
