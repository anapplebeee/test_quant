"""交易领域的稳定合同。

本包不依赖数据库、券商 SDK、Gradio 或回测实现。执行、手动交易和券商适配器
通过显式转换使用这里的对象，避免分别维护相互漂移的订单/成交模型。
"""
from quart.domain.enums import (
    OrderSide,
    OrderStatus,
    RiskDecisionStatus,
    RiskRuleOutcome,
    TradingEnvironment,
)
from quart.domain.executions import ExecutionReport, Fill
from quart.domain.ids import (
    AccountId,
    BrokerOrderId,
    ClientOrderId,
    DecisionId,
    EventId,
    FillId,
    IdempotencyKey,
    IntentId,
    new_id,
    stable_id,
)
from quart.domain.orders import BrokerOrder, OrderIntent, RiskDecision, RiskRuleResult
from quart.domain.state_machine import (
    OrderTransitionError,
    apply_execution_report,
    create_order_from_risk_decision,
    make_execution_report,
)
from quart.domain.time import SHANGHAI_TZ, market_datetime, utc_now

__all__ = [
    "SHANGHAI_TZ",
    "AccountId",
    "BrokerOrder",
    "BrokerOrderId",
    "ClientOrderId",
    "DecisionId",
    "EventId",
    "ExecutionReport",
    "Fill",
    "FillId",
    "IdempotencyKey",
    "IntentId",
    "OrderIntent",
    "OrderSide",
    "OrderStatus",
    "OrderTransitionError",
    "RiskDecision",
    "RiskDecisionStatus",
    "RiskRuleOutcome",
    "RiskRuleResult",
    "TradingEnvironment",
    "apply_execution_report",
    "create_order_from_risk_decision",
    "make_execution_report",
    "market_datetime",
    "new_id",
    "stable_id",
    "utc_now",
]
