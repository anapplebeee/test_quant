"""执行层一致性契约测试。

这两个不变量是整个架构改造的地基，破了就回到"回测一套、实盘另一套"：

1. 回测与实盘**共用** `generate_orders`——不是"两份实现行为相近"。
2. 同一份目标权重下，LiveExecutionModel 与 BacktestExecutionModel 产出的
   **标的集合与方向**必须一致，差异只允许体现在成交价与拒单上。
"""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

from quart.backtest.engine import BacktestEngine, MarketData
from quart.execution import (
    BUY,
    FLAT,
    SELL,
    BacktestExecutionModel,
    ExecutionContext,
    Fees,
    LiveExecutionModel,
    generate_orders,
)
from quart.pipeline import generate_orders as pipeline_generate_orders
from quart.risk.rules import validate_weights


def _ctx(**overrides) -> ExecutionContext:
    prices = pd.Series({"A": 10.0, "B": 20.0, "C": 5.0})
    base = dict(
        date=pd.Timestamp("2024-01-02"),
        targets={"A": 0.5, "B": 0.5},
        equity=100_000.0,
        cash=100_000.0,
        positions={},
        mark_prices=prices,
        exec_prices=prices,
        prev_closes=prices,
        fees=Fees.zero(),
    )
    base.update(overrides)
    return ExecutionContext(**base)


# ---------------------------------------------------------------- 契约 1


def test_pipeline_and_backtest_share_one_implementation():
    """pipeline 的委托生成必须直接复用执行层的函数，不得有私有副本。"""
    src = inspect.getsource(pipeline_generate_orders)
    assert "generate_orders(" in src, "pipeline 未调用 quart.execution.generate_orders"
    # 不得残留自建的整手/预算循环（此前正是两份实现漂移的根源）
    for forbidden in ("// (price * 100)", "sell_proceeds = 0.0", "budget -="):
        assert forbidden not in src, f"pipeline 仍存在私有撮合逻辑: {forbidden}"


def test_engine_delegates_to_execution_layer():
    """回测引擎不得自带撮合实现。"""
    src = inspect.getsource(BacktestEngine)
    assert "generate_orders(" in src
    assert "_rebalance" in src


# ---------------------------------------------------------------- 契约 2


def test_live_and_backtest_agree_on_universe_and_direction():
    """成交价/拒单规则不同，但选中的标的与买卖方向必须一致。"""
    ctx = _ctx()

    bt = generate_orders(ctx, BacktestExecutionModel(Fees.zero()))
    live = generate_orders(ctx, LiveExecutionModel(Fees.zero()))

    bt_sig = sorted((o.symbol, o.side) for o in bt.orders)
    live_sig = sorted((o.symbol, o.side) for o in live.orders)
    assert bt_sig == live_sig
    assert bt_sig, "应产生委托"
    assert all(side == BUY for _, side in bt_sig)


def test_flat_signal_liquidates_everything_in_both_models():
    ctx = _ctx(targets={FLAT: 1.0}, positions={"A": 500, "B": 300, "C": 1000})

    for model in (BacktestExecutionModel(Fees.zero()), LiveExecutionModel(Fees.zero())):
        plan = generate_orders(ctx, model)
        assert {o.symbol for o in plan.orders} == {"A", "B", "C"}
        assert all(o.side == SELL for o in plan.orders)
        assert plan.ending_positions == {}, f"{type(model).__name__} 未清仓"


# ---------------------------------------------------------------- 执行层行为


def test_limit_up_blocks_buy_but_live_model_only_warns():
    # 前收 10 元主板涨停价 11.0，开盘即 11.0 → 买单无法成交
    prices = pd.Series({"A": 11.0})
    prev = pd.Series({"A": 10.0})
    ctx = _ctx(
        targets={"A": 1.0},
        equity=100_000.0,
        cash=100_000.0,
        positions={},
        mark_prices=prev,
        exec_prices=prices,
        prev_closes=prev,
    )

    bt = generate_orders(ctx, BacktestExecutionModel(Fees.zero()))
    assert bt.orders == []
    assert any("涨停" in (o.blocked_reason or "") for o in bt.skipped)

    live_model = LiveExecutionModel(Fees.zero())
    live = generate_orders(ctx, live_model)
    assert live.orders, "实盘不应因昨收涨停就静默撤单"
    assert any("涨停" in w for w in live_model.warnings)


def test_limit_down_blocks_sell_in_backtest():
    prices = pd.Series({"A": 9.0})   # 前收 10，跌停 9.0
    prev = pd.Series({"A": 10.0})
    ctx = _ctx(
        targets={},
        equity=100_000.0,
        cash=0.0,
        positions={"A": 1000},
        mark_prices=prev,
        exec_prices=prices,
        prev_closes=prev,
    )
    plan = generate_orders(ctx, BacktestExecutionModel(Fees.zero()))
    assert plan.orders == []
    assert plan.ending_positions == {"A": 1000}


def test_suspended_stock_is_neither_bought_nor_sold():
    prices = pd.Series({"A": float("nan"), "B": 20.0})
    ctx = _ctx(
        targets={"A": 0.5, "B": 0.5},
        positions={"A": 100},
        mark_prices=prices,
        exec_prices=prices,
        prev_closes=prices,
    )
    plan = generate_orders(ctx, BacktestExecutionModel(Fees.zero()))
    assert all(o.symbol != "A" for o in plan.orders)
    assert any(o.symbol == "A" for o in plan.skipped)


def test_live_model_reserves_fees_so_plan_is_affordable():
    """实盘委托必须预留费用：不能给出"算得出但买不起"的计划。"""
    prices = pd.Series({"A": 10.0})
    fees = Fees(commission_rate=0.00025, commission_min=5.0, stamp_tax_rate=0.0005,
                transfer_fee_rate=0.00001, slippage_rate=0.0)
    ctx = _ctx(
        targets={"A": 1.0},
        equity=100_000.0,
        cash=100_000.0,
        positions={},
        mark_prices=prices,
        exec_prices=prices,
        prev_closes=prices,
        fees=fees,
        cash_buffer=1.0,
    )
    plan = generate_orders(ctx, LiveExecutionModel(fees))
    assert plan.ending_cash >= 0, "预留费用后不应出现负现金"
    total = sum(o.amount + o.fee for o in plan.orders if o.side == BUY)
    assert total <= 100_000.0


def test_generate_orders_does_not_mutate_inputs():
    positions = {"A": 100}
    targets = {"A": 0.5}
    ctx = _ctx(positions=positions, targets=targets, cash=50_000.0, equity=51_000.0)
    generate_orders(ctx, BacktestExecutionModel(Fees.zero()))
    assert positions == {"A": 100}
    assert targets == {"A": 0.5}


# ---------------------------------------------------------------- 风控进回测


def _flat_strategy():
    from quart.strategy.base import BaseStrategy

    class Once(BaseStrategy):
        name = "once"

        def prepare(self, md):
            super().prepare(md)
            self._fired = False

        def target_weights(self, i):
            if self._fired:
                return {}
            self._fired = True
            # 单票 100%，远超 25% 上限
            return {"A": 1.0}

    return Once()


def _md() -> MarketData:
    dates = pd.date_range("2024-01-01", periods=6)
    frames = []
    for symbol, price in (("A", 10.0), ("B", 10.0)):
        frames.append(pd.DataFrame({
            "date": dates, "symbol": symbol,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000_000.0, "amount": 1e7,
        }))
    return MarketData.from_bars(pd.concat(frames, ignore_index=True))


def test_risk_pipeline_is_enforced_inside_backtest():
    """风控必须能在回测内生效——此前它只在实盘路径，回测组合可以违反上限。"""

    def risk_pipeline(targets, prices, equity):
        clean, _violations = validate_weights(targets, prices, equity, max_position_pct=0.25)
        return clean

    md = _md()
    plain = BacktestEngine(md, _flat_strategy(), fees=Fees.zero(), initial_cash=100_000)
    guarded = BacktestEngine(
        md, _flat_strategy(), fees=Fees.zero(), initial_cash=100_000,
        risk_pipeline=risk_pipeline,
    )

    pos_plain = plain.run_result().final_positions
    pos_guarded = guarded.run_result().final_positions

    # 无限风控制：全仓押 A
    assert pos_plain.get("A", 0) > 0
    assert "B" not in pos_plain or pos_plain["B"] == 0

    # 有风控：单票被截断到 25%，组合被迫分散
    value_a = pos_guarded.get("A", 0) * 10.0
    assert value_a <= 100_000 * 0.25 + 10, "单票权重未受风控约束"


def test_risk_pipeline_does_not_break_flat_signal():
    """FLAT（清仓）不经过权重校验：它没有"权重超限"的概念，
    对 {FLAT: 1.0} 做 validate_weights 会把它当成一个名为 __FLAT__ 的标的。"""
    seen: list[dict] = []

    def risk_pipeline(targets, prices, equity):
        seen.append(dict(targets))
        return targets

    from quart.strategy.base import BaseStrategy

    class GoFlatLater(BaseStrategy):
        name = "goflatlater"
        flat_at = 3

        def target_weights(self, i):
            if i < self.flat_at:
                return {"A": 0.5}
            return {FLAT: 1.0}

    engine = BacktestEngine(_md(), GoFlatLater(), fees=Fees.zero(),
                            initial_cash=100_000, risk_pipeline=risk_pipeline)
    result = engine.run_result()

    assert result.final_positions == {}, "FLAT 信号未清仓"
    assert all(FLAT not in t for t in seen), "FLAT 不应进入风控权重校验"


# ---------------------------------------------------------------- 可重复调用


def test_engine_run_is_idempotent():
    """此前 run() 二次调用会重复追加 trades。"""
    engine = BacktestEngine(_md(), _flat_strategy(), fees=Fees.zero(), initial_cash=100_000)
    first = len(engine.run_result().trades)
    second = len(engine.run_result().trades)
    assert first > 0
    assert first == second, "run() 未重置运行态，trades 被重复追加"
