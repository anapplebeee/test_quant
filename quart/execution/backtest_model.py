"""回测执行模型：开盘价成交 + 不利方向滑点 + 涨跌停拒单。"""
from __future__ import annotations

import math

from quart.execution.constraints import is_limit_down, is_limit_up
from quart.execution.fees import Fees
from quart.execution.models import BUY


class BacktestExecutionModel:
    """回测口径的执行模型。

    - 成交价：次日开盘价，按不利方向施加滑点（买入 +slip，卖出 -slip）
    - 拒单：开盘一字涨停（买不进）/ 一字跌停（卖不出）
    - 冲击成本：按 ADV 参与率平方根叠加（impact_coef > 0 时生效）
    """

    def __init__(self, fees: Fees | None = None, enforce_limits: bool = True):
        self.fees = fees or Fees.from_config()
        self.enforce_limits = enforce_limits

    def exec_price(
        self,
        symbol: str,
        side: str,
        base_price: float,
        order_notional: float,
        position_notional: float,
        adv: float,
    ) -> float:
        slip = self.fees.slip_rate(order_notional, adv)
        if side == BUY:
            return base_price * (1 + slip)
        return base_price * (1 - slip)

    def blocked_reason(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
    ) -> str | None:
        if not self.enforce_limits:
            return None
        if not math.isfinite(prev_close) or not math.isfinite(base_price):
            return None
        if side == BUY and is_limit_up(base_price, prev_close, symbol):
            return "开盘涨停，买单无法成交"
        if side != BUY and is_limit_down(base_price, prev_close, symbol):
            return "开盘跌停，卖单无法成交"
        return None


__all__ = ["BacktestExecutionModel"]
