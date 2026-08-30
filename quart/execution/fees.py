"""A 股交易费用模型（回测与实盘计划共用）。

抽取动机：此前费用计算内联在 `backtest/engine.py`，实盘计划路径
(`pipeline.generate_orders`) 完全没有费用，导致回测口径与实盘口径不可比。
"""
from __future__ import annotations

from dataclasses import dataclass

from quart.config import load_config


@dataclass
class Fees:
    """A 股双边差异化费用。

    - 佣金：双边，万 2.5，单笔最低 5 元
    - 印花税：仅卖出，万 5
    - 过户费：双边，十万分之一（沪深两市）
    - 滑点：按不利方向施加（买入 +slip，卖出 -slip）
    - 冲击成本：可选，按参与率平方根叠加（ADV 占比）
    """

    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_rate: float = 0.001
    impact_coef: float = 0.0

    @classmethod
    def from_config(cls) -> "Fees":
        cfg = load_config()["backtest"]
        return cls(
            commission_rate=cfg["commission_rate"],
            commission_min=cfg["commission_min"],
            stamp_tax_rate=cfg["stamp_tax_rate"],
            transfer_fee_rate=cfg["transfer_fee_rate"],
            slippage_rate=cfg["slippage_rate"],
            impact_coef=cfg.get("impact_coef", 0.0),
        )

    @classmethod
    def zero(cls) -> "Fees":
        """零成本口径（用于孪生参照/成本隔离诊断）。"""
        return cls(
            commission_rate=0.0,
            commission_min=0.0,
            stamp_tax_rate=0.0,
            transfer_fee_rate=0.0,
            slippage_rate=0.0,
            impact_coef=0.0,
        )

    def buy_cost(self, amount: float) -> float:
        commission = max(amount * self.commission_rate, self.commission_min)
        return commission + amount * self.transfer_fee_rate

    def sell_cost(self, amount: float) -> float:
        commission = max(amount * self.commission_rate, self.commission_min)
        return commission + amount * self.stamp_tax_rate + amount * self.transfer_fee_rate

    def buy_price(self, open_price: float) -> float:
        return open_price * (1 + self.slippage_rate)

    def sell_price(self, open_price: float) -> float:
        return open_price * (1 - self.slippage_rate)

    def slip_rate(self, notional: float, adv: float) -> float:
        """总滑点率 = 基础滑点 + 冲击成本（按 ADV 参与率平方根）。"""
        base = self.slippage_rate
        if self.impact_coef <= 0 or adv <= 0 or notional <= 0:
            return base
        participation = min(notional / adv, 1.0)
        return base + self.impact_coef * (participation**0.5)


__all__ = ["Fees"]
