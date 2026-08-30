"""内存模拟 Broker：用于订单状态机和 API 接入前联调。"""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime

from quart.broker.models import BrokerFill, BrokerOrder, BrokerOrderRequest, OrderStatus


class PaperBrokerAdapter:
    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._fills: list[BrokerFill] = []

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        request.validate()
        order_id = f"paper_{uuid.uuid4().hex[:12]}"
        order = BrokerOrder(order_id, request, OrderStatus.SUBMITTED)
        self._orders[order_id] = order
        return order

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        order = self._required_order(broker_order_id)
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
            raise ValueError(f"订单当前状态不可撤销: {order.status}")
        canceled = replace(order, status=OrderStatus.CANCELED)
        self._orders[broker_order_id] = canceled
        return canceled

    def apply_fill(
        self,
        broker_order_id: str,
        quantity: int,
        price: float,
        trade_date: str | None = None,
        trade_time: str | None = None,
        broker_fill_id: str | None = None,
    ) -> BrokerFill:
        order = self._required_order(broker_order_id)
        if order.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise ValueError(f"订单当前状态不可成交: {order.status}")
        if quantity <= 0 or price <= 0:
            raise ValueError("成交数量和价格必须为正")
        if quantity > order.remaining_quantity:
            raise ValueError("成交数量超过订单剩余数量")
        new_filled = order.filled_quantity + quantity
        total_amount = order.average_fill_price * order.filled_quantity + price * quantity
        average_price = total_amount / new_filled
        status = OrderStatus.FILLED if new_filled == order.request.quantity else OrderStatus.PARTIALLY_FILLED
        updated = replace(
            order,
            status=status,
            filled_quantity=new_filled,
            average_fill_price=average_price,
        )
        self._orders[broker_order_id] = updated
        fill = BrokerFill(
            broker_fill_id=broker_fill_id or f"paper_fill_{uuid.uuid4().hex[:12]}",
            broker_order_id=broker_order_id,
            symbol=order.request.symbol,
            side=order.request.side.upper(),
            quantity=quantity,
            price=float(price),
            trade_date=trade_date or date.today().isoformat(),
            trade_time=trade_time or datetime.now().strftime("%H:%M:%S"),
            planned_order_id=order.request.planned_order_id,
        )
        self._fills.append(fill)
        return fill

    def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        return self._orders.get(broker_order_id)

    def list_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    def list_fills(self) -> list[BrokerFill]:
        return list(self._fills)

    def _required_order(self, broker_order_id: str) -> BrokerOrder:
        order = self.get_order(broker_order_id)
        if order is None:
            raise KeyError(f"订单不存在: {broker_order_id}")
        return order


__all__ = ["PaperBrokerAdapter"]
