"""组合构建时点上下文。

策略在收盘生成 alpha，Constructor 还需要看到该时点真实账户的当前权重、
可交易集合和已知 ADV，才能正确处理换手、容量和不可交易持仓冻结。本 DTO
由回测和每日信号共同装配，避免策略自行从全局状态猜测账户。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class PortfolioConstructionContext:
    """收盘决策时可获得的账户与流动性信息。"""

    date: pd.Timestamp
    current_weights: pd.Series
    equity: float
    tradable: pd.Index
    adv: pd.Series | None = None
    max_adv_participation: float | None = None


__all__ = ["PortfolioConstructionContext"]
