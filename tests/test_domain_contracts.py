"""ARCH-001：统一领域合同、状态机与旧路径转换测试。"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quart.broker import BrokerOrderRequest, PaperBrokerAdapter
from quart.domain import (
    OrderIntent,
    OrderStatus,
    OrderTransitionError,
    RiskDecision,
    RiskRuleOutcome,
    RiskRuleResult,
    TradingEnvironment,
    apply_execution_report,
    create_order_from_risk_decision,
    make_execution_report,
)
from quart.execution import BUY, OrderPlan
from quart.manual_trading import FillInput, PlannedOrderInput

BUSINESS_TIME = datetime(2026, 8, 31, 7, 0, tzinfo=UTC)


def _intent() -> OrderIntent:
    return OrderIntent.create(
        account_id="account-1",
        environment=TradingEnvironment.PAPER,
        symbol="600000",
        side="BUY",
        quantity=1_000,
        business_time=BUSINESS_TIME,
        source="TEST",
        idempotency_key="intent-key-1",
    )


def test_order_state_machine_tracks_risk_submission_partial_and_full_fill():
    intent = _intent()
    decision = RiskDecision.allow(intent, limit_version="limits-v1", business_time=BUSINESS_TIME)
    order = create_order_from_risk_decision(intent, decision, client_order_id="client-1")
    assert order.status is OrderStatus.RISK_APPROVED

    submitting = make_execution_report(
        order,
        status=OrderStatus.SUBMITTING,
        source="TEST",
        idempotency_key="client-1:submitting",
        business_time=BUSINESS_TIME,
    )
    order = apply_execution_report(order, submitting)
    submitted = make_execution_report(
        order,
        status=OrderStatus.SUBMITTED,
        source="TEST",
        idempotency_key="client-1:submitted",
        broker_order_id="broker-1",
        business_time=BUSINESS_TIME,
    )
    order = apply_execution_report(order, submitted)

    partial = make_execution_report(
        order,
        status=OrderStatus.PARTIALLY_FILLED,
        source="TEST",
        idempotency_key="fill-1",
        cumulative_filled_quantity=400,
        last_filled_quantity=400,
        last_fill_price=10,
        broker_order_id="broker-1",
        business_time=BUSINESS_TIME,
    )
    order = apply_execution_report(order, partial)
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.remaining_quantity == 600
    assert order.average_fill_price == Decimal("10")
    assert apply_execution_report(order, partial) == order

    completed = make_execution_report(
        order,
        status=OrderStatus.FILLED,
        source="TEST",
        idempotency_key="fill-2",
        cumulative_filled_quantity=1_000,
        last_filled_quantity=600,
        last_fill_price=Decimal("10.2"),
        broker_order_id="broker-1",
        business_time=BUSINESS_TIME,
    )
    order = apply_execution_report(order, completed)
    assert order.status is OrderStatus.FILLED
    assert order.average_fill_price == Decimal("10.12")
    assert order.is_terminal


def test_order_state_machine_rejects_skipped_or_inconsistent_transitions():
    intent = _intent()
    decision = RiskDecision.allow(intent, limit_version="limits-v1", business_time=BUSINESS_TIME)
    created = create_order_from_risk_decision(intent, decision, client_order_id="client-2")

    with pytest.raises(OrderTransitionError, match="非法"):
        apply_execution_report(
            created,
            make_execution_report(
                created,
                status=OrderStatus.FILLED,
                source="TEST",
                idempotency_key="invalid-fill",
                cumulative_filled_quantity=1_000,
                last_filled_quantity=1_000,
                last_fill_price=10,
                business_time=BUSINESS_TIME,
            ),
        )

    denied = RiskDecision.deny(
        intent,
        rules=(RiskRuleResult("kill-switch", RiskRuleOutcome.DENY, "manual halt"),),
        limit_version="limits-v1",
        business_time=BUSINESS_TIME,
    )
    denied_order = create_order_from_risk_decision(intent, denied, client_order_id="client-3")
    assert denied_order.status is OrderStatus.DENIED
    assert denied_order.approved_quantity == 0


def test_legacy_execution_manual_and_broker_objects_convert_to_domain_contracts():
    execution_intent = OrderPlan("600000", BUY, 100, 10.0, exec_price=10.1).to_order_intent(
        account_id="research-run-1",
        planned_order_id="order-plan-1",
        business_time=BUSINESS_TIME,
    )
    assert execution_intent.environment is TradingEnvironment.RESEARCH
    assert execution_intent.limit_price == Decimal("10.1")

    planned_intent = PlannedOrderInput("000001", "SELL", 200, 12.3).to_order_intent(
        account_id=7,
        planned_order_id=19,
        business_time=BUSINESS_TIME,
    )
    assert planned_intent.account_id == "7"
    assert planned_intent.planned_order_id == "19"

    manual_fill = FillInput("000001", "SELL", 200, 12.4, "2026-08-31", broker_fill_id="fill-1")
    domain_fill = manual_fill.to_domain_fill(account_id=7)
    assert domain_fill.environment is TradingEnvironment.PAPER
    assert FillInput.from_domain_fill(domain_fill).broker_fill_id == "fill-1"

    broker_request = BrokerOrderRequest(
        "600000",
        "BUY",
        300,
        client_order_id="broker-client-1",
        account_id="account-2",
    )
    broker_intent = broker_request.to_order_intent()
    assert broker_intent.intent_id.startswith("intent_")
    assert broker_intent.idempotency_key == "broker-client-1"


def test_paper_broker_is_idempotent_and_records_normalized_reports():
    broker = PaperBrokerAdapter()
    request = BrokerOrderRequest(
        "600000",
        "BUY",
        100,
        client_order_id="paper-client-1",
        account_id="account-3",
    )
    submitted = broker.submit_order(request)
    assert broker.submit_order(request).broker_order_id == submitted.broker_order_id
    assert [report.status for report in broker.list_execution_reports()] == [
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTING,
        OrderStatus.SUBMITTED,
    ]

    first_fill = broker.apply_fill(
        submitted.broker_order_id,
        100,
        10.0,
        trade_date="2026-08-31",
        broker_fill_id="paper-fill-1",
    )
    assert broker.apply_fill(
        submitted.broker_order_id,
        100,
        10.0,
        trade_date="2026-08-31",
        broker_fill_id="paper-fill-1",
    ) == first_fill
    assert broker.get_domain_order(submitted.broker_order_id).status is OrderStatus.FILLED
