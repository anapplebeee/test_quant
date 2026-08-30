"""端到端每日流水线测试：初始化 → 计划 → 审批 → 成交(含计划外) → 对账 → 次日计划。

对应 MANUAL_TRADING_T1_SYNC_PLAN.md 第 3 节 T+1 日常工作流，
覆盖计划外交易（MANUAL_EXTERNAL 语义：planned_order_id=None）纳入账本。
"""
from __future__ import annotations

import pytest

from quart.execution.models import BUY, SELL
from quart.manual_trading import FillInput, PlannedOrderInput, TradingRepository


def _repository(tmp_path) -> TradingRepository:
    repository = TradingRepository(tmp_path / "trading.db")
    repository.initialize_schema()
    return repository


def test_full_daily_cycle_with_external_fills(tmp_path):
    repo = _repository(tmp_path)

    # ---- T 日收盘：初始化账户（对账快照 as_of = 信号日，视为当日收盘已对账） ----
    repo.initialize_account(
        cash=1_000_000,
        positions={"600000": {"total_quantity": 1_000, "sellable_quantity": 1_000, "cost_price": 10.0}},
        as_of="2026-08-28",
    )
    state_t0 = repo.account_state(as_of="2026-08-28")
    assert state_t0 is not None
    assert state_t0.reconciliation_status == "RECONCILED"

    # ---- T 日：基于已对账状态生成 T+1 计划 ----
    plan_id = repo.create_trade_plan(
        account_id=state_t0.account_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[
            PlannedOrderInput("600519", BUY, 100, 1500.0, 0.15),
            PlannedOrderInput("600000", SELL, 400, 10.5, 0.0),
        ],
        source_run_id="run_test_e2e",
    )

    # 计划本身不改变账户
    unchanged = repo.account_state(as_of="2026-08-28")
    assert unchanged is not None
    assert unchanged.cash_total == 1_000_000

    repo.approve_plan(plan_id)
    detail = repo.plan_detail(plan_id)
    sell_order_id = detail["orders"][0]["planned_order_id"]  # SELL 排序在前
    buy_order_id = detail["orders"][1]["planned_order_id"]

    # ---- T+1 交易日：成交回填 ----
    # 1) 计划内卖出部分成交 300/400
    repo.record_fill(
        state_t0.account_id,
        FillInput("600000", SELL, 300, 10.4, "2026-08-31", planned_order_id=sell_order_id, broker_fill_id="e2e-s1"),
    )
    # 2) 计划内买入全部成交
    repo.record_fill(
        state_t0.account_id,
        FillInput("600519", BUY, 100, 1502.0, "2026-08-31", planned_order_id=buy_order_id, broker_fill_id="e2e-b1"),
    )
    # 3) 计划外临时买入（用户自主交易），必须可记录并纳入账本
    external_fill = repo.record_fill(
        state_t0.account_id,
        FillInput("000001", BUY, 900, 12.0, "2026-08-31", broker_fill_id="e2e-ext1", source="MANUAL_EXTERNAL"),
    )
    assert external_fill > 0

    plan_status = repo.plan_detail(plan_id)["plan"]["status"]
    assert plan_status == "PARTIAL"  # 卖出未全部成交

    # 计划外成交无法自动匹配到订单（没有剩余足够的 APPROVED 订单）
    assert repo.match_planned_order(state_t0.account_id, "000001", BUY, "2026-08-31", 900) is None

    # ---- T+1 收盘：对账（券商快照覆盖账本） ----
    # 账本推导：现金 = 1,000,000 - 300*10.4(卖费省略为0) + ... 这里直接给券商数字
    diff = repo.reconcile(
        account_name="manual",
        as_of="2026-08-31",
        cash_total=761_180.0,
        cash_available=761_180.0,
        cash_withdrawable=761_180.0,
        positions={
            "600000": {"total_quantity": 700, "sellable_quantity": 700, "cost_price": 10.0},
            "600519": {"total_quantity": 100, "sellable_quantity": 0, "cost_price": 1502.0},
            "000001": {"total_quantity": 900, "sellable_quantity": 0, "cost_price": 12.0},
        },
        confirm=True,
        resolution="以券商收盘快照为准；600519/000001 当日买入 T+1 不可卖",
    )
    assert diff.confirmed and diff.reconciliation_id is not None

    state_t1 = repo.account_state(as_of="2026-08-31")
    assert state_t1 is not None
    assert state_t1.reconciliation_status == "RECONCILED"
    # 对账覆盖后：当日买入不可卖，600000 卖出后剩余 700 可卖
    assert state_t1.sellable_positions == {"600000": 700, "600519": 0, "000001": 0}

    # ---- T+1 日生成 T+2 计划：读取已对账状态，可卖数量受限 ----
    state_for_plan = repo.account_state(as_of="2026-08-31")
    assert state_for_plan is not None
    assert state_for_plan.cash_available_to_trade == 761_180.0
    # 卖出计划受可卖数量约束的语义由 order_generator 截断，这里验证账本输入正确
    plan2_id = repo.create_trade_plan(
        account_id=state_for_plan.account_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-31",
        intended_trade_date="2026-09-01",
        orders=[PlannedOrderInput("600000", SELL, 700, 10.5, 0.0)],
        source_run_id="run_test_e2e_day2",
    )
    repo.approve_plan(plan2_id)

    # ---- T+2：卖出成交、执行复盘 ----
    order2_id = repo.plan_detail(plan2_id)["orders"][0]["planned_order_id"]
    repo.record_fill(
        state_for_plan.account_id,
        FillInput("600000", SELL, 700, 10.6, "2026-09-01", planned_order_id=order2_id, broker_fill_id="e2e-s2"),
    )
    assert repo.plan_detail(plan2_id)["plan"]["status"] == "COMPLETED"

    summary = repo.execution_summary(plan_id)[0]
    assert summary["filled_quantity"] == 300
    assert summary["average_fill_price"] == pytest.approx(10.4)
    # 卖出 10.4 低于参考价 10.5 → 正值 = 不利滑点 (1 - 10.4/10.5) * 1e4
    assert summary["slippage_bps"] == pytest.approx(95.238, abs=0.01)

    fills = repo.list_fills("manual", limit=10)
    assert any(row["source"] == "MANUAL_EXTERNAL" for row in fills)
    assert len([row for row in fills if row["trade_date"] == "2026-08-31"]) == 3


def test_external_fill_blocked_when_insufficient_cash_or_sellable(tmp_path):
    repo = _repository(tmp_path)
    repo.initialize_account(cash=1_000, positions={}, as_of="2026-08-28")
    state = repo.account_state(as_of="2026-08-31")
    assert state is not None

    with pytest.raises(ValueError, match="可用资金不足"):
        repo.record_fill(
            state.account_id,
            FillInput("600519", BUY, 100, 1500.0, "2026-08-31", source="MANUAL_EXTERNAL"),
        )

    with pytest.raises(ValueError, match="可卖数量不足"):
        repo.record_fill(
            state.account_id,
            FillInput("600000", SELL, 100, 10.0, "2026-08-31", source="MANUAL_EXTERNAL"),
        )
