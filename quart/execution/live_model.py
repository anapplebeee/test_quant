"""实盘执行模型：参考价委托，不做滑点预测，不因涨跌停拒单。

与回测模型的差异是**有意为之**且必须显式：

1. 实盘无法预知次日开盘价，只能用最新收盘作为参考价；
   真实成交价由市场决定，回测中的滑点是"假设"，实盘中它是"结果"。
2. 实盘不应因"昨日收盘涨停"就撤销委托——次日可能开板。涨跌停只作为
   提示（`warnings`），由人工确认，而不是程序静默拒单。
"""
from __future__ import annotations

import math

from quart.execution.constraints import is_limit_down, is_limit_up
from quart.execution.fees import Fees
from quart.execution.models import BUY


class LiveExecutionModel:
    """实盘/纸面交易口径的执行模型。"""

    def __init__(self, fees: Fees | None = None, note_limits: bool = True):
        self.fees = fees or Fees.from_config()
        self.note_limits = note_limits
        #: 本轮生成的非阻塞提示（由调用方读取后清空）
        self.warnings: list[str] = []

    def exec_price(
        self,
        symbol: str,
        side: str,
        base_price: float,
        order_notional: float,
        position_notional: float,
        adv: float,
    ) -> float:
        """实盘不做成交价预测，按参考价估算名义额与费用。"""
        return float(base_price)

    def blocked_reason(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
    ) -> str | None:
        if not self.note_limits:
            return None
        if not math.isfinite(prev_close) or not math.isfinite(base_price):
            return None
        if side == BUY and is_limit_up(base_price, prev_close, symbol):
            self.warnings.append(f"{symbol}: 昨收涨停，次日开盘可能无法买入，请人工确认")
        elif side != BUY and is_limit_down(base_price, prev_close, symbol):
            self.warnings.append(f"{symbol}: 昨收跌停，次日开盘可能无法卖出，请人工确认")
        return None


__all__ = ["LiveExecutionModel"]
