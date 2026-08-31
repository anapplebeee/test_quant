"""券商兼容模型。

本模块保留既有 Adapter API，规范订单/成交合同位于 :mod:`quart.domain`。
新适配器应先转换为领域对象，再通过 ``ExecutionReport`` 推进订单状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from quart.domain import (
    SHANGHAI_TZ,
    OrderIntent,
    OrderSide,
    OrderStatus,
    TradingEnvironment,
    market_datetime,
    new_id,
    stable_id,
    utc_now,
)
from quart.domain import (
    BrokerOrder as DomainBrokerOrder,
)
from quart.domain import (
    Fill as DomainFill,
)


@dataclass(frozen=True)
class BrokerOrderRequest:
    """旧 BrokerAdapter 入参；``to_order_intent`` 是规范转换入口。"""

    symbol: str
    side: str
    quantity: int
    limit_price: float | None = None
    client_order_id: str | None = None
    planned_order_id: int | None = None
    account_id: str = "paper"
    environment: TradingEnvironment | str = TradingEnvironment.PAPER
    intent_id: str | None = None
    idempotency_key: str | None = None
    reason: str = ""
    business_time: datetime | None = None

    def validate(self) -> None:
        try:
            OrderSide.coerce(self.side)
        except ValueError as exc:
            raise ValueError(f"未知买卖方向: {self.side}") from exc
        if self.quantity <= 0:
            raise ValueError("委托数量必须为正")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("限价必须为正")
        if not str(self.account_id).strip():
            raise ValueError("账户不能为空")

    def normalized(self) -> BrokerOrderRequest:
        """补齐全局 ID 与环境，供幂等提交和领域转换共用。"""
        self.validate()
        client_order_id = self.client_order_id or new_id("client_order")
        intent_id = self.intent_id or stable_id("intent", client_order_id)
        return replace(
            self,
            symbol=self.symbol.strip().upper(),
            side=OrderSide.coerce(self.side).value,
            client_order_id=client_order_id,
            account_id=str(self.account_id).strip(),
            environment=TradingEnvironment.coerce(self.environment),
            intent_id=intent_id,
            idempotency_key=self.idempotency_key or client_order_id,
            business_time=self.business_time or utc_now(),
        )

    def to_order_intent(self) -> OrderIntent:
        request = self.normalized()
        assert request.client_order_id is not None
        assert request.intent_id is not None
        assert request.idempotency_key is not None
        assert request.business_time is not None
        return OrderIntent(
            intent_id=request.intent_id,
            account_id=request.account_id,
            environment=TradingEnvironment.coerce(request.environment),
            symbol=request.symbol,
            side=OrderSide.coerce(request.side),
            quantity=request.quantity,
            business_time=request.business_time,
            source="BROKER_REQUEST",
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            limit_price=request.limit_price,
            planned_order_id=(str(request.planned_order_id) if request.planned_order_id is not None else None),
        )

    def identity_key(self) -> tuple[str, str, int, float | None, int | None, str, str]:
        """用于同一 client_order_id 的重试一致性校验。"""
        return (
            self.symbol.strip().upper(),
            OrderSide.coerce(self.side).value,
            self.quantity,
            self.limit_price,
            self.planned_order_id,
            str(self.account_id).strip(),
            TradingEnvironment.coerce(self.environment).value,
        )


@dataclass(frozen=True)
class BrokerOrder:
    """旧查询视图，内部可附带规范 ``DomainBrokerOrder``。"""

    broker_order_id: str
    request: BrokerOrderRequest
    status: OrderStatus
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    reject_reason: str | None = None
    domain_order: DomainBrokerOrder | None = field(default=None, repr=False, compare=False)

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.request.quantity - self.filled_quantity)

    @property
    def client_order_id(self) -> str | None:
        return self.request.client_order_id

    @property
    def intent_id(self) -> str | None:
        return self.request.intent_id

    @classmethod
    def from_domain(cls, order: DomainBrokerOrder, request: BrokerOrderRequest) -> BrokerOrder:
        if order.broker_order_id is None:
            raise ValueError("只有已获券商订单号的订单可以转换为 BrokerOrder")
        return cls(
            broker_order_id=order.broker_order_id,
            request=request,
            status=order.status,
            filled_quantity=order.filled_quantity,
            average_fill_price=float(order.average_fill_price),
            reject_reason=(
                order.status_reason if order.status in {OrderStatus.DENIED, OrderStatus.REJECTED} else None
            ),
            domain_order=order,
        )


@dataclass(frozen=True)
class BrokerFill:
    """旧成交回报；``to_domain_fill`` 把它规范化为可入账的 ``Fill``。"""

    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    trade_date: str
    trade_time: str | None = None
    planned_order_id: int | None = None
    client_order_id: str | None = None
    intent_id: str | None = None
    account_id: str = "paper"
    environment: TradingEnvironment | str = TradingEnvironment.PAPER
    idempotency_key: str | None = None
    source: str = "BROKER_ADAPTER"
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    other_fee: float = 0.0
    event_id: str | None = None
    domain_fill: DomainFill | None = field(default=None, repr=False, compare=False)

    def to_domain_fill(
        self,
        *,
        account_id: str | None = None,
        source: str | None = None,
    ) -> DomainFill:
        if self.domain_fill is not None:
            return self.domain_fill
        final_account_id = str(account_id or self.account_id).strip()
        if not final_account_id:
            raise ValueError("账户不能为空")
        client_order_id = self.client_order_id or stable_id(
            "client_order", f"{final_account_id}:{self.broker_order_id}"
        )
        intent_id = self.intent_id or stable_id("intent", client_order_id)
        idempotency_key = self.idempotency_key or f"{final_account_id}:{self.broker_fill_id}"
        return DomainFill.create(
            fill_id=stable_id("fill", idempotency_key),
            event_id=self.event_id or stable_id("event", f"fill:{idempotency_key}"),
            client_order_id=client_order_id,
            intent_id=intent_id,
            account_id=final_account_id,
            environment=TradingEnvironment.coerce(self.environment),
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            business_time=market_datetime(self.trade_date, self.trade_time),
            source=source or self.source,
            idempotency_key=idempotency_key,
            broker_order_id=self.broker_order_id,
            broker_fill_id=self.broker_fill_id,
            planned_order_id=(str(self.planned_order_id) if self.planned_order_id is not None else None),
            commission=self.commission,
            stamp_tax=self.stamp_tax,
            transfer_fee=self.transfer_fee,
            other_fee=self.other_fee,
        )

    @classmethod
    def from_domain(cls, fill: DomainFill) -> BrokerFill:
        business_time = fill.business_time.astimezone(SHANGHAI_TZ)
        planned_order_id = (
            int(fill.planned_order_id)
            if fill.planned_order_id is not None and fill.planned_order_id.isdigit()
            else None
        )
        return cls(
            broker_fill_id=fill.broker_fill_id or fill.fill_id,
            broker_order_id=fill.broker_order_id or "",
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=float(fill.price),
            trade_date=business_time.date().isoformat(),
            trade_time=business_time.strftime("%H:%M:%S"),
            planned_order_id=planned_order_id,
            client_order_id=fill.client_order_id,
            intent_id=fill.intent_id,
            account_id=fill.account_id,
            environment=fill.environment,
            idempotency_key=fill.idempotency_key,
            source=fill.source,
            commission=float(fill.commission),
            stamp_tax=float(fill.stamp_tax),
            transfer_fee=float(fill.transfer_fee),
            other_fee=float(fill.other_fee),
            event_id=fill.event_id,
            domain_fill=fill,
        )


__all__ = ["BrokerFill", "BrokerOrder", "BrokerOrderRequest", "OrderStatus"]
