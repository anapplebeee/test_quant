"""回测执行模型：开盘价成交 + 不利方向滑点 + 涨跌停拒单。"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from quart.execution.constraints import is_limit_down, is_limit_up
from quart.execution.fees import Fees
from quart.execution.models import BUY

if TYPE_CHECKING:
    from quart.execution.models import ExecutionContext
    from quart.execution.rule_resolver import ExecutionRuleResolver


class BacktestExecutionModel:
    """回测口径的执行模型。

    - 成交价：次日开盘价，按不利方向施加滑点（买入 +slip，卖出 -slip）
    - 拒单：开盘一字涨停（买不进）/ 一字跌停（卖不出）
    - 冲击成本：按 ADV 参与率平方根叠加（impact_coef > 0 时生效）
    """

    def __init__(
        self,
        fees: Fees | None = None,
        enforce_limits: bool = True,
        rule_resolver: ExecutionRuleResolver | None = None,
    ):
        self.fees = fees or Fees.from_config()
        self.enforce_limits = enforce_limits
        if rule_resolver is None:
            from quart.execution.rule_resolver import ExecutionRuleResolver

            rule_resolver = ExecutionRuleResolver()
        self.rule_resolver = rule_resolver
        self._context: ExecutionContext | None = None

    def bind_context(self, context: ExecutionContext) -> None:
        """由共享订单生成器注入执行日，确保 RuleBook 查询与订单同日。"""
        self._context = context

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
        if self._context is not None:
            rule_reason = self.rule_resolver.blocked_reason(
                symbol, side, base_price, prev_close, self._context.date
            )
            if rule_reason is not None:
                return rule_reason
        if side == BUY and is_limit_up(base_price, prev_close, symbol):
            return "开盘涨停，买单无法成交"
        if side != BUY and is_limit_down(base_price, prev_close, symbol):
            return "开盘跌停，卖单无法成交"
        return None


__all__ = ["BacktestExecutionModel"]
