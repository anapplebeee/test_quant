"""手动交易 T+1 账本与计划闭环测试。"""
from __future__ import annotations

import csv
import json

import pytest

from quart.execution.models import BUY, SELL
from quart.manual_trading import FillInput, PlannedOrderInput, TradingRepository
from quart.manual_trading.io import import_fills_csv


def _repository(tmp_path) -> TradingRepository:
    repository = TradingRepository(tmp_path / "trading.db")
    repository.initialize_schema()
    return repository


def test_initialize_from_legacy_holdings(tmp_path):
    repository = _repository(tmp_path)
    holdings = tmp_path / "holdings.json"
    holdings.write_text(
        json.dumps({"cash": 50_000, "positions": {"600519": 200}}),
        encoding="utf-8",
    )

    snapshot_id = repository.initialize_from_holdings_json(holdings, "2026-08-28")
    state = repository.account_state(as_of="2026-08-28")

    assert snapshot_id > 0
    assert state is not None
    assert state.cash_available_to_trade == 50_000
    assert state.total_positions == {"600519": 200}
    assert state.sellable_positions == {"600519": 200}
    with pytest.raises(ValueError, match="已初始化"):
        repository.initialize_from_holdings_json(holdings, "2026-08-28")


def test_buy_is_not_sellable_until_next_trade_day(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None

    repository.record_fill(
        state.account_id,
        FillInput(
            symbol="600000",
            side=BUY,
            quantity=1_000,
            price=10.0,
            trade_date="2026-08-31",
            broker_fill_id="buy-1",
        ),
    )

    same_day = repository.account_state(as_of="2026-08-31")
    next_day = repository.account_state(as_of="2026-09-01")
    assert same_day is not None and next_day is not None
    assert same_day.total_positions == {"600000": 1_000}
    assert same_day.sellable_positions == {"600000": 0}
    assert next_day.sellable_positions == {"600000": 1_000}

    with pytest.raises(ValueError, match="可卖数量不足"):
        repository.record_fill(
            state.account_id,
            FillInput(
                symbol="600000",
                side=SELL,
                quantity=100,
                price=10.0,
                trade_date="2026-08-31",
            ),
        )


def test_plan_requires_approval_and_tracks_partial_fill(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None
    plan_id = repository.create_trade_plan(
        account_id=state.account_id,
        account_snapshot_id=state.snapshot_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600000", BUY, 1_000, 10.0, 0.1)],
    )
    order_id = repository.plan_detail(plan_id)["orders"][0]["planned_order_id"]

    with pytest.raises(ValueError, match="尚未审批"):
        repository.record_fill(
            state.account_id,
            FillInput("600000", BUY, 500, 10.0, "2026-08-31", planned_order_id=order_id),
        )

    repository.approve_plan(plan_id)
    repository.record_fill(
        state.account_id,
        FillInput(
            "600000",
            BUY,
            400,
            10.0,
            "2026-08-31",
            planned_order_id=order_id,
            broker_fill_id="partial-1",
        ),
    )
    partial = repository.plan_detail(plan_id)
    assert partial["plan"]["status"] == "PARTIAL"
    assert partial["orders"][0]["filled_quantity"] == 400

    repository.record_fill(
        state.account_id,
        FillInput(
            "600000",
            BUY,
            600,
            10.0,
            "2026-08-31",
            planned_order_id=order_id,
            broker_fill_id="partial-2",
        ),
    )
    completed = repository.plan_detail(plan_id)
    assert completed["plan"]["status"] == "COMPLETED"
    assert completed["orders"][0]["status"] == "COMPLETED"


def test_plan_rejects_same_day_execution(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-28")
    assert state is not None
    with pytest.raises(ValueError, match=r"T\+1"):
        repository.create_trade_plan(
            account_id=state.account_id,
            strategy_name="momentum_rotation",
            signal_date="2026-08-28",
            intended_trade_date="2026-08-28",
            orders=[],
        )


def test_duplicate_broker_fill_id_is_rejected(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None
    fill = FillInput("600000", BUY, 100, 10.0, "2026-08-31", broker_fill_id="same-id")
    repository.record_fill(state.account_id, fill)
    with pytest.raises(ValueError, match="成交编号重复"):
        repository.record_fill(state.account_id, fill)


def test_reconcile_only_replaces_state_after_confirm(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(
        cash=50_000,
        positions={"600000": 1_000},
        as_of="2026-08-28",
    )

    preview = repository.reconcile(
        account_name="manual",
        as_of="2026-08-31",
        cash_total=49_500,
        cash_available=49_500,
        cash_withdrawable=49_500,
        positions={"600000": {"total_quantity": 900, "sellable_quantity": 900}},
        confirm=False,
    )
    before = repository.account_state(as_of="2026-08-31")
    assert not preview.confirmed
    assert preview.position_differences
    assert before is not None
    assert before.total_positions == {"600000": 1_000}

    confirmed = repository.reconcile(
        account_name="manual",
        as_of="2026-08-31",
        cash_total=49_500,
        cash_available=49_500,
        cash_withdrawable=49_500,
        positions={"600000": {"total_quantity": 900, "sellable_quantity": 900}},
        confirm=True,
        resolution="以券商收盘快照为准",
    )
    after = repository.account_state(as_of="2026-08-31")
    assert confirmed.confirmed
    assert confirmed.reconciliation_id is not None
    assert after is not None
    assert after.cash_total == 49_500
    assert after.total_positions == {"600000": 900}


def test_import_fills_csv_matches_planned_order(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None
    plan_id = repository.create_trade_plan(
        account_id=state.account_id,
        strategy_name="momentum_rotation",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600000", BUY, 100, 10.0)],
    )
    repository.approve_plan(plan_id)
    order_id = repository.plan_detail(plan_id)["orders"][0]["planned_order_id"]
    csv_path = tmp_path / "fills.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["trade_date", "symbol", "side", "quantity", "price", "planned_order_id", "broker_fill_id"],
        )
        writer.writeheader()
        writer.writerow({
            "trade_date": "2026-08-31",
            "symbol": "600000",
            "side": BUY,
            "quantity": 100,
            "price": 10.0,
            "planned_order_id": order_id,
            "broker_fill_id": "csv-1",
        })

    fill_ids = import_fills_csv(repository, state.account_id, csv_path)
    assert len(fill_ids) == 1
    assert repository.plan_detail(plan_id)["plan"]["status"] == "COMPLETED"


def test_adjustment_survives_plan_approval(tmp_path):
    repository = _repository(tmp_path)
    repository.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    state = repository.account_state(as_of="2026-08-31")
    assert state is not None
    plan_id = repository.create_trade_plan(
        account_id=state.account_id,
        strategy_name="momentum_rotation",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600000", BUY, 1_000, 10.0)],
    )
    order_id = repository.plan_detail(plan_id)["orders"][0]["planned_order_id"]
    repository.adjust_planned_order(order_id, 500, "人工降低风险")
    repository.approve_plan(plan_id)

    order = repository.plan_detail(plan_id)["orders"][0]
    assert order["approved_quantity"] == 500
    assert order["adjustment_reason"] == "人工降低风险"
