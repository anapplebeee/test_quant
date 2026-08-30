"""执行层：目标权重 → 委托计划。

回测与实盘共用 `generate_orders()`，差异只由 `ExecutionModel` 注入。
"""
from __future__ import annotations

from quart.execution.backtest_model import BacktestExecutionModel
from quart.execution.constraints import FLAT, A_SHARE_LOT
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

__all__ = [
    "A_SHARE_LOT",
    "BUY",
    "SELL",
    "FLAT",
    "BacktestExecutionModel",
    "ExecutionContext",
    "ExecutionModel",
    "Fees",
    "LiveExecutionModel",
    "OrderPlan",
    "RebalancePlan",
    "generate_orders",
]
