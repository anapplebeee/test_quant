"""订单意图、风控决策和订单查询状态的纯领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from quart.domain.enums import (
    OrderSide,
    OrderStatus,
    RiskDecisionStatus,
    RiskRuleOutcome,
    TradingEnvironment,
)
from quart.domain.ids import new_id, require_id, stable_id
from quart.domain.time import require_aware, utc_now


def _required_text(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    return text


def _positive_decimal(value: Decimal | float | int | str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if result <= 0:
        raise ValueError(f"{field_name} 必须为正")
    return result


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """风控前、与券商无关的委托意图。"""

    intent_id: str
    account_id: str
    environment: TradingEnvironment
    symbol: str
    side: OrderSide
    quantity: int
    business_time: datetime
    source: str
    idempotency_key: str
    reason: str = ""
    limit_price: Decimal | float | int | str | None = None
    planned_order_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", require_id(self.intent_id, "intent_id"))
        object.__setattr__(self, "account_id", require_id(self.account_id, "account_id"))
        environment = TradingEnvironment.coerce(self.environment)
        object.__setattr__(self, "environment", environment)
        symbol = _required_text(self.symbol, "symbol").upper()
        if any(char.isspace() for char in symbol):
            raise ValueError("symbol 不能包含空白字符")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", OrderSide.coerce(self.side))
        if self.quantity <= 0:
            raise ValueError("quantity 必须为正")
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "idempotency_key", require_id(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", _positive_decimal(self.limit_price, "limit_price"))
        if self.planned_order_id is not None:
            object.__setattr__(self, "planned_order_id", require_id(self.planned_order_id, "planned_order_id"))

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        environment: TradingEnvironment | str,
        symbol: str,
        side: OrderSide | str,
        quantity: int,
        business_time: datetime | None = None,
        source: str,
        reason: str = "",
        limit_price: Decimal | float | int | str | None = None,
        planned_order_id: str | None = None,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> OrderIntent:
        final_intent_id = intent_id or new_id("intent")
        normalized_environment = TradingEnvironment.coerce(environment)
        normalized_side = OrderSide.coerce(side)
        return cls(
            intent_id=final_intent_id,
            account_id=account_id,
            environment=normalized_environment,
            symbol=symbol,
            side=normalized_side,
            quantity=quantity,
            business_time=business_time or utc_now(),
            source=source,
            idempotency_key=idempotency_key or final_intent_id,
            reason=reason,
            limit_price=limit_price,
            planned_order_id=planned_order_id,
        )


@dataclass(frozen=True, slots=True)
class RiskRuleResult:
    rule_id: str
    outcome: RiskRuleOutcome
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", require_id(self.rule_id, "rule_id"))
        object.__setattr__(self, "outcome", RiskRuleOutcome.coerce(self.outcome))
        object.__setattr__(self, "message", _required_text(self.message, "message"))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """风控对一笔订单意图的可审计决定。"""

    decision_id: str
    intent_id: str
    account_id: str
    environment: TradingEnvironment
    status: RiskDecisionStatus
    requested_quantity: int
    approved_quantity: int
    rules: tuple[RiskRuleResult, ...]
    limit_version: str
    business_time: datetime
    source: str
    idempotency_key: str
    reason: str = ""
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", require_id(self.decision_id, "decision_id"))
        object.__setattr__(self, "intent_id", require_id(self.intent_id, "intent_id"))
        object.__setattr__(self, "account_id", require_id(self.account_id, "account_id"))
        object.__setattr__(self, "environment", TradingEnvironment.coerce(self.environment))
        status = RiskDecisionStatus.coerce(self.status)
        object.__setattr__(self, "status", status)
        if self.requested_quantity <= 0:
            raise ValueError("requested_quantity 必须为正")
        if self.approved_quantity < 0 or self.approved_quantity > self.requested_quantity:
            raise ValueError("approved_quantity 必须介于 0 和 requested_quantity 之间")
        if status is RiskDecisionStatus.ALLOW and self.approved_quantity != self.requested_quantity:
            raise ValueError("ALLOW 必须批准全部数量")
        if status is RiskDecisionStatus.ADJUST and not 0 < self.approved_quantity < self.requested_quantity:
            raise ValueError("ADJUST 必须批准部分正数量")
        if status is RiskDecisionStatus.DENY and self.approved_quantity != 0:
            raise ValueError("DENY 的 approved_quantity 必须为 0")
        rules = tuple(self.rules)
        if not all(isinstance(rule, RiskRuleResult) for rule in rules):
            raise TypeError("rules 必须全部为 RiskRuleResult")
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "limit_version", _required_text(self.limit_version, "limit_version"))
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "idempotency_key", require_id(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))

    @classmethod
    def for_intent(
        cls,
        intent: OrderIntent,
        *,
        status: RiskDecisionStatus | str,
        approved_quantity: int,
        rules: tuple[RiskRuleResult, ...] = (),
        limit_version: str,
        source: str = "RISK_ENGINE",
        reason: str = "",
        decision_id: str | None = None,
        idempotency_key: str | None = None,
        business_time: datetime | None = None,
    ) -> RiskDecision:
        normalized_status = RiskDecisionStatus.coerce(status)
        decision_key = idempotency_key or (
            f"{intent.idempotency_key}:risk:{limit_version}:{normalized_status}:{approved_quantity}"
        )
        return cls(
            decision_id=decision_id or stable_id("decision", decision_key),
            intent_id=intent.intent_id,
            account_id=intent.account_id,
            environment=intent.environment,
            status=normalized_status,
            requested_quantity=intent.quantity,
            approved_quantity=approved_quantity,
            rules=rules,
            limit_version=limit_version,
            business_time=business_time or utc_now(),
            source=source,
            idempotency_key=decision_key,
            reason=reason,
        )

    @classmethod
    def allow(
        cls,
        intent: OrderIntent,
        *,
        rules: tuple[RiskRuleResult, ...] = (),
        limit_version: str,
        source: str = "RISK_ENGINE",
        reason: str = "",
        idempotency_key: str | None = None,
        business_time: datetime | None = None,
    ) -> RiskDecision:
        return cls.for_intent(
            intent,
            status=RiskDecisionStatus.ALLOW,
            approved_quantity=intent.quantity,
            rules=rules,
            limit_version=limit_version,
            source=source,
            reason=reason,
            idempotency_key=idempotency_key,
            business_time=business_time,
        )

    @classmethod
    def adjust(
        cls,
        intent: OrderIntent,
        *,
        approved_quantity: int,
        rules: tuple[RiskRuleResult, ...],
        limit_version: str,
        source: str = "RISK_ENGINE",
        reason: str = "",
        idempotency_key: str | None = None,
        business_time: datetime | None = None,
    ) -> RiskDecision:
        return cls.for_intent(
            intent,
            status=RiskDecisionStatus.ADJUST,
            approved_quantity=approved_quantity,
            rules=rules,
            limit_version=limit_version,
            source=source,
            reason=reason,
            idempotency_key=idempotency_key,
            business_time=business_time,
        )

    @classmethod
    def deny(
        cls,
        intent: OrderIntent,
        *,
        rules: tuple[RiskRuleResult, ...],
        limit_version: str,
        source: str = "RISK_ENGINE",
        reason: str = "",
        idempotency_key: str | None = None,
        business_time: datetime | None = None,
    ) -> RiskDecision:
        return cls.for_intent(
            intent,
            status=RiskDecisionStatus.DENY,
            approved_quantity=0,
            rules=rules,
            limit_version=limit_version,
            source=source,
            reason=reason,
            idempotency_key=idempotency_key,
            business_time=business_time,
        )


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """订单生命周期的规范查询模型，状态只能由 ExecutionReport 推进。"""

    client_order_id: str
    intent_id: str
    account_id: str
    environment: TradingEnvironment
    symbol: str
    side: OrderSide
    requested_quantity: int
    approved_quantity: int
    business_time: datetime
    source: str
    idempotency_key: str
    limit_price: Decimal | float | int | str | None = None
    planned_order_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_fill_price: Decimal | float | int | str = Decimal("0")
    status_reason: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime | None = None
    last_event_id: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", require_id(self.client_order_id, "client_order_id"))
        object.__setattr__(self, "intent_id", require_id(self.intent_id, "intent_id"))
        object.__setattr__(self, "account_id", require_id(self.account_id, "account_id"))
        object.__setattr__(self, "environment", TradingEnvironment.coerce(self.environment))
        symbol = _required_text(self.symbol, "symbol").upper()
        if any(char.isspace() for char in symbol):
            raise ValueError("symbol 不能包含空白字符")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", OrderSide.coerce(self.side))
        if self.requested_quantity <= 0:
            raise ValueError("requested_quantity 必须为正")
        if self.approved_quantity < 0 or self.approved_quantity > self.requested_quantity:
            raise ValueError("approved_quantity 必须介于 0 和 requested_quantity 之间")
        object.__setattr__(self, "business_time", require_aware(self.business_time, "business_time"))
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "idempotency_key", require_id(self.idempotency_key, "idempotency_key"))
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", _positive_decimal(self.limit_price, "limit_price"))
        if self.planned_order_id is not None:
            object.__setattr__(self, "planned_order_id", require_id(self.planned_order_id, "planned_order_id"))
        status = OrderStatus.coerce(self.status)
        object.__setattr__(self, "status", status)
        if self.broker_order_id is not None:
            object.__setattr__(self, "broker_order_id", require_id(self.broker_order_id, "broker_order_id"))
        if self.filled_quantity < 0 or self.filled_quantity > self.approved_quantity:
            raise ValueError("filled_quantity 超出已批准数量")
        average = Decimal(str(self.average_fill_price))
        if average < 0:
            raise ValueError("average_fill_price 不能为负")
        if self.filled_quantity > 0 and average <= 0:
            raise ValueError("有成交时 average_fill_price 必须为正")
        object.__setattr__(self, "average_fill_price", average)
        object.__setattr__(self, "created_at", require_aware(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "updated_at",
            require_aware(self.updated_at or self.business_time, "updated_at"),
        )
        if self.last_event_id is not None:
            object.__setattr__(self, "last_event_id", require_id(self.last_event_id, "last_event_id"))
        if self.version < 0:
            raise ValueError("version 不能为负")
        if status is OrderStatus.PARTIALLY_FILLED and not 0 < self.filled_quantity < self.approved_quantity:
            raise ValueError("PARTIALLY_FILLED 必须有部分成交")
        if status is OrderStatus.FILLED and self.filled_quantity != self.approved_quantity:
            raise ValueError("FILLED 必须等于已批准数量")
        if status is OrderStatus.DENIED and self.approved_quantity != 0:
            raise ValueError("DENIED 订单不能有已批准数量")
        if status not in (OrderStatus.CREATED, OrderStatus.DENIED) and self.approved_quantity <= 0:
            raise ValueError("非拒绝订单必须有已批准数量")

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.approved_quantity - self.filled_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.DENIED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        }

    @classmethod
    def from_intent(
        cls,
        intent: OrderIntent,
        decision: RiskDecision,
        *,
        client_order_id: str | None = None,
        source: str | None = None,
    ) -> BrokerOrder:
        if decision.intent_id != intent.intent_id:
            raise ValueError("RiskDecision 与 OrderIntent 不匹配")
        if decision.account_id != intent.account_id or decision.environment != intent.environment:
            raise ValueError("RiskDecision 的账户或环境与 OrderIntent 不匹配")
        return cls(
            client_order_id=client_order_id or stable_id("client_order", intent.intent_id),
            intent_id=intent.intent_id,
            account_id=intent.account_id,
            environment=intent.environment,
            symbol=intent.symbol,
            side=intent.side,
            requested_quantity=intent.quantity,
            approved_quantity=decision.approved_quantity,
            business_time=intent.business_time,
            source=source or intent.source,
            idempotency_key=intent.idempotency_key,
            limit_price=intent.limit_price,
            planned_order_id=intent.planned_order_id,
        )


__all__ = ["BrokerOrder", "OrderIntent", "RiskDecision", "RiskRuleResult"]
