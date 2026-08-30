"""与具体券商 SDK 无关的订单和成交模型。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from quart.execution.models import BUY, SELL


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class BrokerOrderRequest:
    symbol: str
    side: str
    quantity: int
    limit_price: float | None = None
    client_order_id: str | None = None
    planned_order_id: int | None = None

    def validate(self) -> None:
        if self.side.upper() not in (BUY, SELL):
            raise ValueError(f"未知买卖方向: {self.side}")
        if self.quantity <= 0:
            raise ValueError("委托数量必须为正")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("限价必须为正")


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    request: BrokerOrderRequest
    status: OrderStatus
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    reject_reason: str | None = None

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.request.quantity - self.filled_quantity)


@dataclass(frozen=True)
class BrokerFill:
    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    trade_date: str
    trade_time: str | None = None
    planned_order_id: int | None = None


__all__ = ["BrokerFill", "BrokerOrder", "BrokerOrderRequest", "OrderStatus"]
