"""订单状态机：所有状态变化都由标准化 ExecutionReport 驱动。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

from quart.domain.enums import OrderStatus
from quart.domain.executions import ExecutionReport
from quart.domain.orders import BrokerOrder, OrderIntent, RiskDecision


class OrderTransitionError(ValueError):
    """订单状态转换或回报内容不符合合同。"""


_ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.RISK_APPROVED, OrderStatus.DENIED}),
    OrderStatus.RISK_APPROVED: frozenset({OrderStatus.SUBMITTING}),
    OrderStatus.SUBMITTING: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.DENIED: frozenset(),
}


def create_order_from_risk_decision(
    intent: OrderIntent,
    decision: RiskDecision,
    *,
    client_order_id: str | None = None,
    source: str | None = None,
) -> BrokerOrder:
    """把风控决定显式转换为 CREATED → RISK_APPROVED/DENIED 状态事件。"""
    order = BrokerOrder.from_intent(intent, decision, client_order_id=client_order_id, source=source)
    return apply_execution_report(order, ExecutionReport.from_risk_decision(order, decision))


def make_execution_report(
    order: BrokerOrder,
    *,
    status: OrderStatus | str,
    source: str,
    idempotency_key: str,
    cumulative_filled_quantity: int | None = None,
    last_filled_quantity: int = 0,
    last_fill_price: Decimal | float | int | str | None = None,
    average_fill_price: Decimal | float | int | str | None = None,
    broker_order_id: str | None = None,
    reason: str = "",
    event_id: str | None = None,
    business_time=None,
) -> ExecutionReport:
    return ExecutionReport.for_order(
        order,
        status=status,
        source=source,
        idempotency_key=idempotency_key,
        cumulative_filled_quantity=cumulative_filled_quantity,
        last_filled_quantity=last_filled_quantity,
        last_fill_price=last_fill_price,
        average_fill_price=average_fill_price,
        broker_order_id=broker_order_id,
        reason=reason,
        event_id=event_id,
        business_time=business_time,
    )


def apply_execution_report(order: BrokerOrder, report: ExecutionReport) -> BrokerOrder:
    """校验并应用一条回报；重复应用最后一条 event_id 是幂等的。"""
    _validate_identity(order, report)
    if report.event_id == order.last_event_id:
        return order

    allowed = _ALLOWED_TRANSITIONS[order.status]
    if report.status not in allowed:
        raise OrderTransitionError(f"非法订单状态转换: {order.status} -> {report.status}")

    _validate_broker_order_id(order, report)
    filled_quantity, average_fill_price = _next_fill_state(order, report)
    _validate_status_quantity(order, report, filled_quantity)

    status_reason = report.reason or order.status_reason
    return replace(
        order,
        status=report.status,
        broker_order_id=report.broker_order_id or order.broker_order_id,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        status_reason=status_reason,
        updated_at=report.business_time,
        last_event_id=report.event_id,
        version=order.version + 1,
    )


def _validate_identity(order: BrokerOrder, report: ExecutionReport) -> None:
    for field_name in ("client_order_id", "intent_id", "account_id", "environment"):
        if getattr(order, field_name) != getattr(report, field_name):
            raise OrderTransitionError(f"ExecutionReport 的 {field_name} 与订单不匹配")


def _validate_broker_order_id(order: BrokerOrder, report: ExecutionReport) -> None:
    if order.broker_order_id and report.broker_order_id and order.broker_order_id != report.broker_order_id:
        raise OrderTransitionError("ExecutionReport 的 broker_order_id 与订单不匹配")


def _next_fill_state(order: BrokerOrder, report: ExecutionReport) -> tuple[int, Decimal]:
    fill_statuses = {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
    if report.status not in fill_statuses:
        if report.cumulative_filled_quantity != order.filled_quantity or report.last_filled_quantity != 0:
            raise OrderTransitionError("非成交回报不能改变累计成交数量")
        if report.last_fill_price is not None or report.average_fill_price is not None:
            raise OrderTransitionError("非成交回报不能包含成交价格")
        return order.filled_quantity, cast(Decimal, order.average_fill_price)

    delta = report.cumulative_filled_quantity - order.filled_quantity
    if delta <= 0:
        raise OrderTransitionError("成交回报的累计成交数量必须递增")
    if report.last_filled_quantity != delta:
        raise OrderTransitionError("last_filled_quantity 必须等于本次累计成交增量")
    if report.last_fill_price is None and report.average_fill_price is None:
        raise OrderTransitionError("成交回报必须提供成交价或累计均价")

    if report.average_fill_price is not None:
        average = cast(Decimal, report.average_fill_price)
    else:
        assert report.last_fill_price is not None
        total_amount = (
            cast(Decimal, order.average_fill_price) * order.filled_quantity
            + cast(Decimal, report.last_fill_price) * delta
        )
        average = total_amount / report.cumulative_filled_quantity
    return report.cumulative_filled_quantity, average


def _validate_status_quantity(
    order: BrokerOrder,
    report: ExecutionReport,
    filled_quantity: int,
) -> None:
    if filled_quantity > order.approved_quantity:
        raise OrderTransitionError("累计成交数量超过已批准数量")
    if report.status is OrderStatus.RISK_APPROVED and order.approved_quantity <= 0:
        raise OrderTransitionError("RISK_APPROVED 订单必须有已批准数量")
    if report.status is OrderStatus.DENIED and order.approved_quantity != 0:
        raise OrderTransitionError("DENIED 订单必须没有已批准数量")
    if report.status is OrderStatus.PARTIALLY_FILLED and not 0 < filled_quantity < order.approved_quantity:
        raise OrderTransitionError("PARTIALLY_FILLED 必须有部分成交")
    if report.status is OrderStatus.FILLED and filled_quantity != order.approved_quantity:
        raise OrderTransitionError("FILLED 必须等于已批准数量")
    if report.status is OrderStatus.REJECTED and filled_quantity != 0:
        raise OrderTransitionError("REJECTED 订单不能包含成交")


__all__ = [
    "OrderTransitionError",
    "apply_execution_report",
    "create_order_from_risk_decision",
    "make_execution_report",
]
