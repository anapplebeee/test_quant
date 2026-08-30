"""随机基线策略（随机信号的定标参照）。

为什么它是核心库的一部分
--------------------------
随机基线是区分"策略 alpha 为负"与"引擎有缺陷"的唯一标尺。README 里
"随机 Top20 也能 +4.9%±4.8%"这条结论，正是靠它才能把策略定位从
"CAGR 显著"修正为"风险调整后 alpha（Sharpe +0.4）"。

此前 `RandomTopKStrategy` 被复制了 **4 份**（`baseline_random.py`、
`diag_random_decomp.py`、`diag_random_period.py`、测试内各一份），且已出现
分叉（`diag_random_period.py` 默认 top_k=20）。基线副本分叉意味着
"同一个基线在不同脚本里给出不同答案"，定标结论随之失效。

改动本文件等同于改动全部诊断口径，会直接影响 README 的置信结论。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.strategy.base import BaseStrategy
from quart.strategy.filters import apply_liquidity

#: 默认年度交易日数（与 metrics.TRADING_DAYS 一致，此处避免循环导入）
TRADING_DAYS = 243


class RandomTopKStrategy(BaseStrategy):
    """每 rebalance_days 从流动性合格池随机等权抽取 top_k 只，不做任何择时。

    必须与真实策略走**完全相同**的流动性/可交易口径，否则对比失去意义：
    "随机选股 + 真实成本"对比"策略选股 + 真实成本"，差异才能归因到选股 alpha。
    """

    name = "random_topk"

    PARAMS_SCHEMA = {
        "top_k": (int, 10, "随机持仓数量"),
        "rebalance_days": (int, 5, "调仓周期（交易日）"),
        "max_weight_pct": (float, 0.15, "单票权重上限"),
        "min_avg_amount": ((int, float, type(None)), None, "流动性门槛"),
        "liquidity_days": (int, 20, "流动性回看窗口"),
        "min_price": ((int, float, type(None)), None, "最低价过滤"),
        "seed": (int, 0, "随机种子"),
    }

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        p = self.params
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.warmup = self.liquidity_days + 1
        self._rng = np.random.default_rng(int(p.get("seed", 0)))
        self._next_rebalance = self.warmup

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._require_md()
        if i < self.warmup or i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        tradable = self.tradable_symbols(md, i)
        if len(tradable) == 0:
            return {}
        # 占位分数：借用 apply_liquidity 做与真实策略完全相同的流动性口径过滤
        holder = pd.Series(1.0, index=tradable)
        pool = apply_liquidity(
            holder, md, i, self.min_avg_amount, self.liquidity_days, self.min_price
        )
        if len(pool) < self.top_k:
            return {}
        pick = self._rng.choice(pool.index.to_numpy(), size=self.top_k, replace=False)
        weight = min(1.0 / len(pick), self.max_weight)
        return {sym: weight for sym in pick}


def k_day_rebal(rets: pd.DataFrame, k: int) -> pd.Series:
    """每 k 日再平衡的等权组合，返回以每段末日为索引的段收益率序列。

    用于把"每日再平衡的等权宇宙"折算成 k 日调仓口径，从而与真实策略的
    换手频率可比（等权宇宙每日再平衡含波动收割伪影，不能直接比超额）。
    """
    n = len(rets)
    if n == 0:
        return pd.Series(dtype=float)
    seg_sum = np.log1p(rets).groupby(np.arange(n) // k).sum()  # NaN 跳过=停牌不计息
    seg_ret = np.expm1(seg_sum.mean(axis=1))  # 段内复利 -> 跨股票等权
    end_labels = [rets.index[min((g + 1) * k, n) - 1] for g in seg_ret.index]
    seg_ret.index = pd.DatetimeIndex(end_labels)
    return seg_ret


__all__ = ["TRADING_DAYS", "RandomTopKStrategy", "k_day_rebal"]
