"""内存模拟 Broker：用领域状态机验证订单回报与成交同步。"""
from __future__ import annotations

from datetime import date

from quart.broker.models import BrokerFill, BrokerOrder, BrokerOrderRequest, OrderStatus
from quart.domain import (
    BrokerOrder as DomainBrokerOrder,
)
from quart.domain import (
    ExecutionReport,
    RiskDecision,
    apply_execution_report,
    make_execution_report,
    market_datetime,
    new_id,
    stable_id,
)
from quart.domain import (
    Fill as DomainFill,
)


class PaperBrokerAdapter:
    """内存 Adapter，保持旧 API，同时由 ``ExecutionReport`` 驱动规范状态。"""

    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._domain_orders: dict[str, DomainBrokerOrder] = {}
        self._orders_by_client: dict[str, str] = {}
        self._fills: list[BrokerFill] = []
        self._domain_fills: list[DomainFill] = []
        self._fills_by_broker_id: dict[str, BrokerFill] = {}
        self._reports: list[ExecutionReport] = []

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        normalized = request.normalized()
        assert normalized.client_order_id is not None
        existing_order_id = self._orders_by_client.get(normalized.client_order_id)
        if existing_order_id is not None:
            existing = self._required_order(existing_order_id)
            if existing.request.identity_key() != normalized.identity_key():
                raise ValueError("同一 client_order_id 的委托内容不一致")
            return existing

        intent = normalized.to_order_intent()
        decision = RiskDecision.allow(
            intent,
            limit_version="paper-compat-v1",
            source="PAPER_BROKER",
            reason="模拟盘兼容风控放行",
        )
        domain_order = DomainBrokerOrder.from_intent(
            intent,
            decision,
            client_order_id=normalized.client_order_id,
            source="PAPER_BROKER",
        )
        domain_order = self._apply_report(
            domain_order,
            ExecutionReport.from_risk_decision(domain_order, decision),
        )
        domain_order = self._apply_report(
            domain_order,
            make_execution_report(
                domain_order,
                status=OrderStatus.SUBMITTING,
                source="PAPER_BROKER",
                idempotency_key=f"{normalized.idempotency_key}:submitting",
            ),
        )
        broker_order_id = new_id("paper_order")
        domain_order = self._apply_report(
            domain_order,
            make_execution_report(
                domain_order,
                status=OrderStatus.SUBMITTED,
                source="PAPER_BROKER",
                idempotency_key=f"{normalized.idempotency_key}:submitted",
                broker_order_id=broker_order_id,
            ),
        )
        return self._store_order(domain_order, normalized)

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        order = self._required_domain_order(broker_order_id)
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.DENIED,
        ):
            raise ValueError(f"订单当前状态不可撤销: {order.status}")
        updated = self._apply_report(
            order,
            make_execution_report(
                order,
                status=OrderStatus.CANCELED,
                source="PAPER_BROKER",
                idempotency_key=f"{order.idempotency_key}:cancel:{order.version}",
                reason="模拟撤单",
            ),
        )
        return self._store_order(updated, self._required_order(broker_order_id).request)

    def apply_fill(
        self,
        broker_order_id: str,
        quantity: int,
        price: float,
        trade_date: str | None = None,
        trade_time: str | None = None,
        broker_fill_id: str | None = None,
    ) -> BrokerFill:
        fill_key = broker_fill_id or new_id("paper_fill")
        existing_fill = self._fills_by_broker_id.get(fill_key)
        if existing_fill is not None:
            if (
                existing_fill.broker_order_id == broker_order_id
                and existing_fill.quantity == quantity
                and existing_fill.price == float(price)
            ):
                return existing_fill
            raise ValueError("同一 broker_fill_id 的成交内容不一致")

        order = self._required_domain_order(broker_order_id)
        if order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise ValueError(f"订单当前状态不可成交: {order.status}")
        if quantity <= 0 or price <= 0:
            raise ValueError("成交数量和价格必须为正")
        if quantity > order.remaining_quantity:
            raise ValueError("成交数量超过订单剩余数量")

        new_filled_quantity = order.filled_quantity + quantity
        next_status = (
            OrderStatus.FILLED
            if new_filled_quantity == order.approved_quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        event_time = market_datetime(trade_date or date.today().isoformat(), trade_time)
        report = make_execution_report(
            order,
            status=next_status,
            source="PAPER_BROKER",
            idempotency_key=f"paper-fill:{fill_key}",
            cumulative_filled_quantity=new_filled_quantity,
            last_filled_quantity=quantity,
            last_fill_price=price,
            broker_order_id=broker_order_id,
            business_time=event_time,
        )
        updated = self._apply_report(order, report)
        legacy_order = self._store_order(updated, self._required_order(broker_order_id).request)
        domain_fill = DomainFill.create(
            fill_id=stable_id("fill", f"{updated.account_id}:{fill_key}"),
            event_id=report.event_id,
            client_order_id=updated.client_order_id,
            intent_id=updated.intent_id,
            account_id=updated.account_id,
            environment=updated.environment,
            symbol=updated.symbol,
            side=updated.side,
            quantity=quantity,
            price=price,
            business_time=event_time,
            source="PAPER_BROKER",
            idempotency_key=f"{updated.account_id}:{fill_key}",
            broker_order_id=broker_order_id,
            broker_fill_id=fill_key,
            planned_order_id=updated.planned_order_id,
        )
        fill = BrokerFill.from_domain(domain_fill)
        self._domain_fills.append(domain_fill)
        self._fills.append(fill)
        self._fills_by_broker_id[fill_key] = fill
        assert legacy_order.broker_order_id == broker_order_id
        return fill

    def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        return self._orders.get(broker_order_id)

    def list_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    def list_fills(self) -> list[BrokerFill]:
        return list(self._fills)

    def list_execution_reports(self) -> list[ExecutionReport]:
        return list(self._reports)

    def get_domain_order(self, broker_order_id: str) -> DomainBrokerOrder | None:
        return self._domain_orders.get(broker_order_id)

    def list_domain_fills(self) -> list[DomainFill]:
        return list(self._domain_fills)

    def _apply_report(self, order: DomainBrokerOrder, report: ExecutionReport) -> DomainBrokerOrder:
        updated = apply_execution_report(order, report)
        self._reports.append(report)
        return updated

    def _store_order(self, order: DomainBrokerOrder, request: BrokerOrderRequest) -> BrokerOrder:
        if order.broker_order_id is None:
            raise ValueError("PaperBroker 订单缺少 broker_order_id")
        legacy = BrokerOrder.from_domain(order, request)
        self._domain_orders[order.broker_order_id] = order
        self._orders[order.broker_order_id] = legacy
        self._orders_by_client[order.client_order_id] = order.broker_order_id
        return legacy

    def _required_order(self, broker_order_id: str) -> BrokerOrder:
        order = self.get_order(broker_order_id)
        if order is None:
            raise KeyError(f"订单不存在: {broker_order_id}")
        return order

    def _required_domain_order(self, broker_order_id: str) -> DomainBrokerOrder:
        order = self.get_domain_order(broker_order_id)
        if order is None:
            raise KeyError(f"订单不存在: {broker_order_id}")
        return order


__all__ = ["PaperBrokerAdapter"]
