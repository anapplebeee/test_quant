"""标准化订单回报和不可重复入账的成交合同。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from quart.domain.enums import OrderSide, OrderStatus, RiskDecisionStatus, TradingEnvironment
from quart.domain.ids import new_id, require_id, stable_id
from quart.domain.orders import BrokerOrder, RiskDecision
from quart.domain.time import require_aware, utc_now


def _required_text(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    return text


def _non_negative_decimal(value: Decimal | float | int | str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if result < 0:
        raise ValueError(f"{field_name} 不能为负")
    return result


def _positive_decimal(value: Decimal | float | int | str, field_name: str) -> Decimal:
    result = _non_negative_decimal(value, field_name)
    if result <= 0:
        raise ValueError(f"{field_name} 必须为正")
    return result


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """外部或内部产生的单一订单状态推进事件。"""

    event_id: str
    client_order_id: str
    intent_id: str
    account_id: str
    environment: TradingEnvironment
    status: OrderStatus
    cumulative_filled_quantity: int
    business_time: datetime
    source: str
    idempotency_key: str
    broker_order_id: str | None = None
    last_filled_quantity: int = 0
    last_fill_price: Decimal | float | int | str | None = None
    average_fill_price: Decimal | float | int | str | None = None
    reason: str = ""
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_id(self.event_id, "event_id"))
        object.__setattr__(self, "client_order_id", require_id(self.client_order_id, "client_order_id"))
        object.__setattr__(self, "intent_id", require_id(self.intent_id, "intent_id"))
        object.__setattr__(self, "account_id", require_id(self.account_id, "account_id"))
        object.__setattr__(self, "environment", TradingEnvironment.coerce(self.environment))
        object.__setattr__(self, "status", OrderStatus.coerce(self.status))
        if self.cumulative_filled_quantity < 0:
            raise ValueError("cumulative_filled_quantity 不能为负")
        if self.last_filled_quantity < 0:
            raise ValueError("last_filled_quantity 不能为负")
        if self.last_filled_quantity > self.cumulative_filled_quantity:
            raise ValueError("last_filled_quantity 不能大于累计成交")
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "idempotency_key", require_id(self.idempotency_key, "idempotency_key"))
        if self.broker_order_id is not None:
            object.__setattr__(self, "broker_order_id", require_id(self.broker_order_id, "broker_order_id"))
        if self.last_fill_price is not None:
            object.__setattr__(self, "last_fill_price", _positive_decimal(self.last_fill_price, "last_fill_price"))
        if self.average_fill_price is not None:
            object.__setattr__(self, "average_fill_price", _positive_decimal(self.average_fill_price, "average_fill_price"))
        if self.last_filled_quantity == 0 and self.last_fill_price is not None:
            raise ValueError("last_filled_quantity 为 0 时不能提供 last_fill_price")
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))

    @classmethod
    def for_order(
        cls,
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
        business_time: datetime | None = None,
    ) -> ExecutionReport:
        return cls(
            event_id=event_id or stable_id("event", idempotency_key),
            client_order_id=order.client_order_id,
            intent_id=order.intent_id,
            account_id=order.account_id,
            environment=order.environment,
            status=OrderStatus.coerce(status),
            cumulative_filled_quantity=(
                order.filled_quantity if cumulative_filled_quantity is None else cumulative_filled_quantity
            ),
            business_time=business_time or utc_now(),
            source=source,
            idempotency_key=idempotency_key,
            broker_order_id=broker_order_id,
            last_filled_quantity=last_filled_quantity,
            last_fill_price=last_fill_price,
            average_fill_price=average_fill_price,
            reason=reason,
        )

    @classmethod
    def from_risk_decision(cls, order: BrokerOrder, decision: RiskDecision) -> ExecutionReport:
        if decision.intent_id != order.intent_id:
            raise ValueError("RiskDecision 与 BrokerOrder 不匹配")
        status = (
            OrderStatus.DENIED
            if decision.status is RiskDecisionStatus.DENY
            else OrderStatus.RISK_APPROVED
        )
        key = f"{decision.idempotency_key}:order-transition"
        return cls.for_order(
            order,
            status=status,
            source=decision.source,
            idempotency_key=key,
            reason=decision.reason,
            business_time=decision.business_time,
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """可安全去重并进入账本的真实成交。"""

    fill_id: str
    event_id: str
    client_order_id: str
    intent_id: str
    account_id: str
    environment: TradingEnvironment
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal | float | int | str
    business_time: datetime
    source: str
    idempotency_key: str
    broker_order_id: str | None = None
    broker_fill_id: str | None = None
    planned_order_id: str | None = None
    commission: Decimal | float | int | str = Decimal("0")
    stamp_tax: Decimal | float | int | str = Decimal("0")
    transfer_fee: Decimal | float | int | str = Decimal("0")
    other_fee: Decimal | float | int | str = Decimal("0")
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", require_id(self.fill_id, "fill_id"))
        object.__setattr__(self, "event_id", require_id(self.event_id, "event_id"))
        object.__setattr__(self, "client_order_id", require_id(self.client_order_id, "client_order_id"))
        object.__setattr__(self, "intent_id", require_id(self.intent_id, "intent_id"))
        object.__setattr__(self, "account_id", require_id(self.account_id, "account_id"))
        object.__setattr__(self, "environment", TradingEnvironment.coerce(self.environment))
        symbol = _required_text(self.symbol, "symbol").upper()
        if any(char.isspace() for char in symbol):
            raise ValueError("symbol 不能包含空白字符")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", OrderSide.coerce(self.side))
        if self.quantity <= 0:
            raise ValueError("quantity 必须为正")
        object.__setattr__(self, "price", _positive_decimal(self.price, "price"))
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "idempotency_key", require_id(self.idempotency_key, "idempotency_key"))
        for field_name in ("broker_order_id", "broker_fill_id", "planned_order_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, require_id(value, field_name))
        for field_name in ("commission", "stamp_tax", "transfer_fee", "other_fee"):
            object.__setattr__(self, field_name, _non_negative_decimal(getattr(self, field_name), field_name))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))

    @property
    def amount(self) -> Decimal:
        return cast(Decimal, self.price) * self.quantity

    @property
    def total_fee(self) -> Decimal:
        return (
            cast(Decimal, self.commission)
            + cast(Decimal, self.stamp_tax)
            + cast(Decimal, self.transfer_fee)
            + cast(Decimal, self.other_fee)
        )

    @classmethod
    def create(
        cls,
        *,
        client_order_id: str,
        intent_id: str,
        account_id: str,
        environment: TradingEnvironment | str,
        symbol: str,
        side: OrderSide | str,
        quantity: int,
        price: Decimal | float | int | str,
        business_time: datetime,
        source: str,
        idempotency_key: str,
        event_id: str | None = None,
        fill_id: str | None = None,
        broker_order_id: str | None = None,
        broker_fill_id: str | None = None,
        planned_order_id: str | None = None,
        commission: Decimal | float | int | str = Decimal("0"),
        stamp_tax: Decimal | float | int | str = Decimal("0"),
        transfer_fee: Decimal | float | int | str = Decimal("0"),
        other_fee: Decimal | float | int | str = Decimal("0"),
    ) -> Fill:
        final_fill_id = fill_id or new_id("fill")
        return cls(
            fill_id=final_fill_id,
            event_id=event_id or stable_id("event", idempotency_key),
            client_order_id=client_order_id,
            intent_id=intent_id,
            account_id=account_id,
            environment=TradingEnvironment.coerce(environment),
            symbol=symbol,
            side=OrderSide.coerce(side),
            quantity=quantity,
            price=price,
            business_time=business_time,
            source=source,
            idempotency_key=idempotency_key,
            broker_order_id=broker_order_id,
            broker_fill_id=broker_fill_id,
            planned_order_id=planned_order_id,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            other_fee=other_fee,
        )


__all__ = ["ExecutionReport", "Fill"]
