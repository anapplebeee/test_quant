"""基准构建：与策略标的域匹配的等权基准。

为什么需要：策略实盘只买主板（排除科创/创业板/ST，见 config exclude_*），
而 000300 指数成分含 18% 科创/创业板（2026-08-28 构成：主板 247/创业 33/科创 20），
直接与指数比存在风格错配。等权基准用"与策略完全相同的股票池 + 每日等权再平衡"
构建，是衡量选股 alpha 的正确参照。

注意：等权基准为无成本、每日再平衡的假想组合，收益含幸存者偏差（股票池为
当前在市股票），仅作 alpha 归因参照，非可投资组合。
"""
from __future__ import annotations

import pandas as pd

from quart.backtest.engine import MarketData


def equal_weight_daily_returns(bars: pd.DataFrame) -> pd.Series:
    """股票池等权日收益序列（横截面等权 mean）。

    bars 应为已过滤（排除科创/创业/ST）的原始 bar 数据。
    返回以 date 为索引的日收益 Series。
    """
    md = MarketData.from_bars(bars)
    closes = md.closes
    rets = closes.pct_change(fill_method=None).iloc[1:]
    ew = rets.mean(axis=1).dropna()
    ew.name = "ew_ret"
    return ew


def equal_weight_benchmark(equity: pd.Series, bars: pd.DataFrame) -> pd.Series:
    """构造与回测区间对齐的等权基准净值曲线（初始值 = equity 初始值）。"""
    ew_ret = equal_weight_daily_returns(bars)
    ew_ret = ew_ret.reindex(equity.index).ffill().fillna(0.0)
    start_val = float(equity.iloc[0]) if len(equity) else 1.0
    bench = start_val * (1.0 + ew_ret).cumprod()
    bench.name = "equal_weight_benchmark"
    return bench
