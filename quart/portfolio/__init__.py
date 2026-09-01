"""独立组合构建层。

策略只负责生成横截面 alpha；最终目标权重必须由这里的 Constructor 在
组合约束下生成。这样回测、每日信号和后续 Paper/实盘可以复用同一份权重
语义与审计结果。
"""
from quart.portfolio.constructor import (
    ConstraintUsage,
    PortfolioConstraints,
    PortfolioConstructionInput,
    PortfolioConstructionResult,
    PortfolioConstructor,
    PortfolioInfeasibleError,
)
from quart.portfolio.context import PortfolioConstructionContext

__all__ = [
    "ConstraintUsage",
    "PortfolioConstraints",
    "PortfolioConstructionContext",
    "PortfolioConstructionInput",
    "PortfolioConstructionResult",
    "PortfolioConstructor",
    "PortfolioInfeasibleError",
]
