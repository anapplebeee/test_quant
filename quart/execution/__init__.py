"""执行层：目标权重 → 委托计划。

回测与实盘共用 `generate_orders()`，差异只由 `ExecutionModel` 注入。
"""
from __future__ import annotations

from quart.execution.attribution import (
    ExecutionAttributionSummary,
    attribute_execution,
    attribute_paper_account,
)
from quart.execution.backtest_model import BacktestExecutionModel
from quart.execution.constraints import A_SHARE_LOT, FLAT
from quart.execution.fees import Fees
from quart.execution.live_model import LiveExecutionModel
from quart.execution.models import (
    BUY,
    SELL,
    ExecutionContext,
    ExecutionModel,
    OrderPlan,
    RebalancePlan,
)
from quart.execution.order_generator import generate_orders
from quart.execution.paper_calibration import (
    PaperExecutionCalibration,
    calibrate_paper_account,
    calibrate_paper_execution,
)
from quart.execution.price_scenarios import PRICE_MODES, PriceScenarioResult, resolve_execution_prices
from quart.execution.rule_resolver import ExecutionRuleResolver, ResolvedTradeRule

__all__ = [
    "A_SHARE_LOT",
    "BUY",
    "FLAT",
    "PRICE_MODES",
    "SELL",
    "BacktestExecutionModel",
    "ExecutionAttributionSummary",
    "ExecutionContext",
    "ExecutionModel",
    "ExecutionRuleResolver",
    "Fees",
    "LiveExecutionModel",
    "OrderPlan",
    "PaperExecutionCalibration",
    "PriceScenarioResult",
    "RebalancePlan",
    "ResolvedTradeRule",
    "attribute_execution",
    "attribute_paper_account",
    "calibrate_paper_account",
    "calibrate_paper_execution",
    "generate_orders",
    "resolve_execution_prices",
]
