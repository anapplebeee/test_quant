"""阶段 F：券商 Adapter 回报 → 交易账本统一写入闭环测试。

覆盖：PaperBroker 订单状态机 → BrokerFill 回报 → sync_broker_fills
统一入账（与人工录入同一 record_fill 管线），计划状态推进，重复编号保护。
"""
from __future__ import annotations

import pytest

from quart.broker.models import BrokerOrderRequest
from quart.broker.paper import PaperBrokerAdapter
from quart.broker.sync import sync_broker_fills
from quart.execution.models import BUY
from quart.manual_trading import PlannedOrderInput, TradingRepository


def _approved_plan(repo: TradingRepository, tmp_path) -> tuple[int, str]:
    repo.initialize_account(cash=1_000_000, positions={}, as_of="2026-08-28")
    state = repo.account_state(as_of="2026-08-28")
    assert state is not None
    plan_id = repo.create_trade_plan(
        account_id=state.account_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600519", BUY, 100, 1500.0, 0.15)],
    )
    repo.approve_plan(plan_id)
    return state.account_id, plan_id


def test_paper_broker_fills_are_synced_to_ledger(tmp_path):
    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    account_id, plan_id = _approved_plan(repo, tmp_path)
    order = repo.plan_detail(plan_id)["orders"][0]
    order_id = int(order["planned_order_id"])

    adapter = PaperBrokerAdapter()
    submitted = adapter.submit_order(
        BrokerOrderRequest(
            symbol=order["symbol"],
            side=order["side"],
            quantity=100,
            limit_price=1500.0,
            client_order_id=f"{plan_id}:{order_id}",
            planned_order_id=order_id,
        )
    )
    assert submitted.status.value == "SUBMITTED"
    adapter.apply_fill(submitted.broker_order_id, 100, 1501.0,
                       trade_date="2026-08-31", broker_fill_id="paper-1")

    fill_ids = sync_broker_fills(repo, account_id, adapter.list_fills(), source="PAPER_BROKER")
    assert len(fill_ids) == 1

    # 计划状态推进为 COMPLETED；账本持仓更新；来源标记 PAPER_BROKER
    assert repo.plan_detail(plan_id)["plan"]["status"] == "COMPLETED"
    state = repo.account_state(as_of="2026-08-31")
    assert state is not None
    assert state.total_positions == {"600519": 100}
    fills = repo.list_fills("manual", limit=5)
    assert fills[0]["source"] == "PAPER_BROKER_ESTIMATED_FEES"
    assert fills[0]["broker_fill_id"] == "paper-1"


def test_duplicate_broker_fill_is_rejected_on_sync(tmp_path):
    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    account_id, plan_id = _approved_plan(repo, tmp_path)
    order = repo.plan_detail(plan_id)["orders"][0]

    adapter = PaperBrokerAdapter()
    submitted = adapter.submit_order(
        BrokerOrderRequest(symbol=order["symbol"], side=order["side"], quantity=100,
                           planned_order_id=int(order["planned_order_id"]))
    )
    adapter.apply_fill(submitted.broker_order_id, 100, 1500.0,
                       trade_date="2026-08-31", broker_fill_id="dup-1")
    sync_broker_fills(repo, account_id, adapter.list_fills())
    # 同一成交编号再次同步 → 整批拒绝（与人工导入一致）
    with pytest.raises(ValueError, match="成交编号重复"):
        sync_broker_fills(repo, account_id, adapter.list_fills())


def test_unapproved_plan_blocked_at_paper_trade_api(tmp_path, monkeypatch):
    """paper_trade_action 只允许 APPROVED 计划（API 订单状态机门禁）。"""
    from api.manual_trading_api import paper_trade_action, repository as api_repo
    from quart.config import PROJECT_ROOT

    # 用临时库隔离（monkeypatch manual_settings 指向 tmp）
    import api.manual_trading_api as module

    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    repo.initialize_account(cash=1_000_000, positions={}, as_of="2026-08-28")
    state = repo.account_state(as_of="2026-08-28")
    assert state is not None
    plan_id = repo.create_trade_plan(
        account_id=state.account_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600519", BUY, 100, 1500.0)],
    )
    monkeypatch.setattr(module, "manual_settings", lambda: (tmp_path / "trading.db", "manual"))
    assert "仅 APPROVED" in paper_trade_action(plan_id)
