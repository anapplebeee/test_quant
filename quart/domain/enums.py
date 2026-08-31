"""领域枚举，统一研究、模拟盘与实盘的语义。"""
from __future__ import annotations

from enum import StrEnum


class TradingEnvironment(StrEnum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"

    @classmethod
    def coerce(cls, value: TradingEnvironment | str) -> TradingEnvironment:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().lower())


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def coerce(cls, value: OrderSide | str) -> OrderSide:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    DENIED = "DENIED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"

    @classmethod
    def coerce(cls, value: OrderStatus | str) -> OrderStatus:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


class RiskDecisionStatus(StrEnum):
    ALLOW = "ALLOW"
    ADJUST = "ADJUST"
    DENY = "DENY"

    @classmethod
    def coerce(cls, value: RiskDecisionStatus | str) -> RiskDecisionStatus:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


class RiskRuleOutcome(StrEnum):
    PASS = "PASS"
    ADJUST = "ADJUST"
    DENY = "DENY"

    @classmethod
    def coerce(cls, value: RiskRuleOutcome | str) -> RiskRuleOutcome:
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.DENIED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    }
)


__all__ = [
    "TERMINAL_ORDER_STATUSES",
    "OrderSide",
    "OrderStatus",
    "RiskDecisionStatus",
    "RiskRuleOutcome",
    "TradingEnvironment",
]
