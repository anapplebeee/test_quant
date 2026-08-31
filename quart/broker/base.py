"""券商适配器最小契约。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from quart.broker.models import BrokerFill, BrokerOrder, BrokerOrderRequest
from quart.domain import ExecutionReport


@runtime_checkable
class BrokerAdapter(Protocol):
    """真实券商与模拟券商必须实现的统一边界。"""

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        ...

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        ...

    def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        ...

    def list_orders(self) -> list[BrokerOrder]:
        ...

    def list_fills(self) -> list[BrokerFill]:
        ...

    def list_execution_reports(self) -> list[ExecutionReport]:
        """返回归一化状态回报；订单状态只能由这些回报推进。"""
        ...


__all__ = ["BrokerAdapter"]
