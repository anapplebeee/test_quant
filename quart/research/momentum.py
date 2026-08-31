"""路径感知动量因子。

这些因子对应研报中最容易用日线数据复现的一组动量定义。模块只负责
``DataFrame(index=date, columns=symbol)`` 的因子计算，不持有交易状态，
因此可同时被因子研究页、策略和 walk-forward 回测复用。

所有输入只使用当日及之前的收盘价。``skip_days`` 用于跳过最近几日，
例如 ``window=120, skip_days=20`` 表示 ``t-120`` 到 ``t-20`` 的收益。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData


def simple_momentum(
    closes: pd.DataFrame,
    window: int = 120,
    skip_days: int = 0,
) -> pd.DataFrame:
    """区间收益动量。"""
    window = int(window)
    skip_days = int(skip_days)
    if window < 1 or skip_days < 0:
        raise ValueError("window 必须 >=1，skip_days 必须 >=0")
    prices = closes.shift(skip_days)
    return prices.pct_change(window, fill_method=None)


def rank_momentum(
    closes: pd.DataFrame,
    window: int = 120,
    skip_days: int = 20,
) -> pd.DataFrame:
    """横截面百分位 RankMom。

    排名因子保留了报告的截面排序口径；在 ``top-k`` 策略中它与原始区间
    收益的选股顺序相同，但对模型训练和跨期组合因子更稳定。
    """
    raw = simple_momentum(closes, window=window, skip_days=skip_days)
    return raw.rank(axis=1, pct=True, method="average")


def signed_smooth(
    closes: pd.DataFrame,
    window: int = 240,
    skip_days: int = 0,
) -> pd.DataFrame:
    """带方向的路径平滑度：累计收益 / 逐日绝对收益之和。

    与绝对值 ER 不同，持续下跌的路径会得到负值，避免把下跌趋势误判为
    高质量上涨趋势。分母为零时返回 NaN。
    """
    window = int(window)
    skip_days = int(skip_days)
    if window < 1 or skip_days < 0:
        raise ValueError("window 必须 >=1，skip_days 必须 >=0")
    prices = closes.shift(skip_days)
    net = prices.pct_change(window, fill_method=None)
    daily = prices.pct_change(fill_method=None)
    path = daily.abs().rolling(window, min_periods=window).sum()
    return net.div(path.replace(0, np.nan))


def remove_limit_up_momentum(
    closes: pd.DataFrame,
    window: int = 240,
    skip_days: int = 20,
    limit_up_threshold: float = 0.095,
) -> pd.DataFrame:
    """剔除涨停日贡献后的复合收益动量。

    触及阈值的正收益日被视为涨停日并置零，其余收益复合计算。阈值只是
    日线数据下的近似；正式研究应接入按板块、ST 状态和除权口径生成的
    ``is_limit_up`` 字段，避免把公司行为跳空误判为涨停。
    """
    window = int(window)
    skip_days = int(skip_days)
    threshold = float(limit_up_threshold)
    if window < 1 or skip_days < 0:
        raise ValueError("window 必须 >=1，skip_days 必须 >=0")
    if not 0 < threshold < 1:
        raise ValueError("limit_up_threshold 必须在 (0,1) 内")

    prices = closes.shift(skip_days)
    daily = prices.pct_change(fill_method=None)
    adjusted = daily.mask(daily >= threshold, 0.0)
    # 对负收益使用 log1p 复合，避免 rolling.apply(np.prod) 的慢路径；停牌
    # 缺口保留 NaN，不把不可交易日伪造成零收益。
    log_returns = np.log1p(adjusted.where(adjusted > -1))
    cumulative = log_returns.rolling(window, min_periods=window).sum()
    return np.expm1(cumulative)


def compute_momentum_factor(
    md: MarketData,
    mode: str = "simple",
    window: int = 120,
    skip_days: int = 0,
    limit_up_threshold: float = 0.095,
) -> pd.DataFrame:
    """按名称计算策略可用的路径动量因子。"""
    mode = str(mode).lower()
    if mode == "simple":
        return simple_momentum(md.closes, window, skip_days)
    if mode == "rank":
        return rank_momentum(md.closes, window, skip_days)
    if mode == "smooth":
        return signed_smooth(md.closes, window, skip_days)
    if mode in {"remove_limit_up", "remove_uplimit"}:
        return remove_limit_up_momentum(
            md.closes, window, skip_days, limit_up_threshold
        )
    raise ValueError(
        f"unknown momentum mode: {mode}; "
        "可用 simple/rank/smooth/remove_limit_up"
    )


__all__ = [
    "compute_momentum_factor",
    "rank_momentum",
    "remove_limit_up_momentum",
    "signed_smooth",
    "simple_momentum",
]
