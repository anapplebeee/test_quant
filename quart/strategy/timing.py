"""指数多因子打分择时（研报 R4：广发《125个经典技术指标择时分析》思想的工程化实现）。

核心结论移植（R4，沪深300 2005-2019 回测）：
1. 价格反转类指标长期无效（A股以趋势市为主），打分体系中直接排除；
2. "因子打分"（类别内投票→类别间投票）优于逻辑回归：收益率、回撤、盈亏比全面占优；
3. 多头仓位管理：以"看多类别占比"决定仓位档位，比纯多空二值切换更平滑。

本实现适配现有数据面（MarketData 仅有基准收盘 + 个股日线面板，无指数级
OHLCV/北向/两融），因此信号源选材：
- 价格动量类：bench_close vs MA20、MA5 vs MA20、MA20 vs MA60（R4 动量类
  年化中位数 18.6%，为主力类别）；
- 大盘/广度类：个股面板聚合——收盘价在 MA20 上方的个股占比（ADVPO/ADVR 思想，
  R4 大盘类年化中位数 13.5%）及其自身趋势；
- 量能类：市场总成交额 MA5 vs MA20（MAAMT 为 R4 成交量类最优，年化 29.6%）；
- 价量类：bench_close vs 市场 VWAP20（VWAP 为 R4 价量类最优之一，年化 23.6%）。

输出：分级目标仓位 exposure ∈ {0, 1/levels, ..., 1}（预计算全序列，与
regime_flat_series 同样的"prepare 时预计算"约定，回测与信号单点一致）。
exposure=0 时策略空仓；0<exposure<1 时权重按比例缩放（余下留现金，
order_generator 按 weight*equity 计算目标市值，天然支持）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData


def _category_vote(signals: list[pd.Series]) -> pd.Series:
    """类别内投票：成员信号均值（NaN 成员剔除），全空返回 NaN。"""
    if not signals:
        return pd.Series(dtype=float)
    frame = pd.concat(signals, axis=1)
    return frame.mean(axis=1)


def score_timing_exposure(
    md: MarketData,
    levels: int = 3,
    breadth_ma_window: int = 20,
) -> pd.Series | None:
    """计算分级择时仓位序列（0~1）。

    Args:
        md: 行情面板（需含 benchmark_close；amounts/volumes 可选，缺失时
            对应类别自动剔除而非报错）。
        levels: 档位数（2=全仓/空仓，3=加半仓档）。
        breadth_ma_window: 广度指标自身均线窗口。

    Returns:
        exposure 序列（index 与 benchmark_close 对齐）；无基准数据时返回 None。
    """
    if md.benchmark_close is None:
        return None
    close = md.benchmark_close

    # ---- 价格动量类（R4 动量类为主力）----
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    momentum_signals = [
        (close > ma20).astype(float),
        (ma5 > ma20).astype(float),
        (ma20 > ma60).astype(float),
    ]

    # ---- 大盘/广度类（个股面板聚合）----
    breadth_signals: list[pd.Series] = []
    if md.closes is not None and not md.closes.empty:
        above = md.closes > md.closes.rolling(breadth_ma_window).mean()
        # 仅统计当日有收盘的个股，避免停牌缺口稀释占比
        valid = md.closes.notna()
        breadth = (above & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
        breadth = breadth.astype(float)
        breadth_signals.append((breadth > 0.5).astype(float))
        breadth_signals.append((breadth > breadth.rolling(breadth_ma_window).mean()).astype(float))

    # ---- 量能类（市场总成交额均线，MAAMT 思想）----
    volume_signals: list[pd.Series] = []
    if md.amounts is not None and not md.amounts.empty:
        total_amount = md.amounts.sum(axis=1)
        volume_signals.append(
            (total_amount.rolling(5).mean() > total_amount.rolling(20).mean()).astype(float)
        )

    # ---- 价量类（市场 VWAP：总成交额/总成交量，R4 VWAP 思想）----
    pv_signals: list[pd.Series] = []
    if (
        md.amounts is not None
        and md.volumes is not None
        and not md.amounts.empty
        and not md.volumes.empty
    ):
        total_amount = md.amounts.sum(axis=1)
        total_vol = md.volumes.sum(axis=1).replace(0, np.nan)
        # 市场滚动 VWAP 必须是 Σamount / Σvolume 的滚动比值，不能把
        # 每日 VWAP 做简单均值，否则低成交量交易日会被等权放大。
        mkt_vwap = (
            total_amount.rolling(20, min_periods=20).sum()
            / total_vol.rolling(20, min_periods=20).sum()
        )
        pv_signals.append((close > mkt_vwap).astype(float))

    # 类别间投票：价格反转类按 R4 结论直接排除（A股趋势市中长期无效）
    category_votes = [
        _category_vote(momentum_signals),
        _category_vote(breadth_signals),
        _category_vote(volume_signals),
        _category_vote(pv_signals),
    ]
    category_votes = [v for v in category_votes if not v.dropna().empty]
    if not category_votes:
        return None

    score = pd.concat(category_votes, axis=1).mean(axis=1)

    # ---- 分级仓位（R4 原文口径：看多类别占比 = 仓位）----
    # 例：4 个类别中 3 个看多 → 仓位 0.75，再对齐到 levels 档位栅格。
    # 2 档时退化为 R4 的纯多空二值；3 档即 R4 测得夏普 1.02 的"分3档"配置。
    category_frame = pd.concat(category_votes, axis=1)
    # 类别自身尚未预热时不应被当作看空类别计入分母。
    bull = category_frame.gt(0).where(category_frame.notna())
    bull_ratio = bull.mean(axis=1)
    if levels <= 2:
        exposure = (bull_ratio > 0.5).astype(float)
    else:
        step = 1.0 / (levels - 1)
        exposure = (bull_ratio / step).round() * step
        exposure = exposure.clip(0.0, 1.0)

    # 预热期（信号未就绪）一律空仓，避免半数据状态下误判
    exposure[score.isna()] = 0.0
    exposure.name = "timing_exposure"
    return exposure
