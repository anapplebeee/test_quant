"""拥挤度风险预警层（RESEARCH-003 方向四）。

背景
----
RESEARCH-002 把 ``speculative_crowding20_neg`` 当作 Alpha 因子在组合层失败
（容量不足）。方向四换用法：拥挤度不作为收益来源，而是作为**风险预警信号**——
当交易热度显著超越基本面支撑时降低仓位/权重。

本模块提供：
- ``amount_share``：个股成交额占全市场比例（拥挤的热度维度）。
- ``crowding_indicators``：60 日滚动 z、60 日时间窗经验分位（拥挤水平）、
  20 日加速度；分位用逐日滑动窗口向量化计算，避免 O(T·W·N) 逐列排名。
- ``fundamental_view_panel``：最近披露盈利增速（profit_yoy）的截面分位面板
  （PIT 语义：披露可用日之前不可见，其后持有到下一报告期生效）。
- ``bad_crowding_gap``：拥挤分位 − 基本面分位，>0 表示"坏拥挤"
  （交易热度超越基本面支撑；中金"好/坏拥挤"框架的代理）。
- ``rolling_adaptive_threshold``：滚动 3 年（约 750 交易日）90 分位自适应阈值。
- ``crowding_trigger``：每日首次突破阈值且加速度为正 => 预警事件。

评估口径保持 RESEARCH-002 门禁一致的谨慎：阈值/窗口是研究假设，
2023+ 段只允许观察一次，不得回改。
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

#: 拥挤分位的滚动窗口（天）
CROWDING_WINDOW = 60
#: 加速度窗口（天）
ACCEL_WINDOW = 20
#: 自适应阈值滚动窗口：约 3 个自然年
THRESHOLD_WINDOW = 750
#: 阈值分位
THRESHOLD_QUANTILE = 0.90


def amount_share(md) -> pd.DataFrame:
    """个股成交额占全市场比例（%），index=dates × columns=symbols。

    停牌日成交额缺失填 0（无交易热度 = 极端的低拥挤），避免 NaN 传染。
    """
    amt = md.amounts.astype("float64").fillna(0.0)
    total = amt.sum(axis=1).replace(0, np.nan)
    return amt.div(total, axis=0) * 100.0


def _rolling_pctile_panel(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """面板逐列滚动经验分位（向量化滑动窗口）。

    对每个日期 t 取窗口 [t-window+1, t]，将窗口 × 全截面的比较一次成型：
    percentile(t, s) = mean(panel[w, s] >= panel[t, s]) over 窗口内有效行。
    """
    arr = panel.to_numpy(dtype="float64")
    n, p = arr.shape
    out = np.full_like(arr, np.nan)
    for t in range(window - 1, n):
        win = arr[max(0, t - window + 1): t + 1]
        cur = win[-1]
        valid_col = ~np.isnan(cur)
        if not valid_col.any():
            continue
        den = (~np.isnan(win)).sum(axis=0).astype("float64")
        greater = (win[:, valid_col] >= cur[valid_col]).sum(axis=0).astype("float64")
        with np.errstate(invalid="ignore", divide="ignore"):
            out[t, valid_col] = np.where(
                den[valid_col] > 0, greater / np.maximum(den[valid_col], 1), np.nan
            )
    return pd.DataFrame(out, index=panel.index, columns=panel.columns)


def crowding_indicators(md, *, window: int = CROWDING_WINDOW) -> dict[str, pd.DataFrame]:
    """返回拥挤度指标面板集合（index=dates × columns=symbols）。"""
    share = amount_share(md)
    mean = share.rolling(window, min_periods=max(10, window // 3)).mean()
    std = share.rolling(window, min_periods=max(10, window // 3)).std().replace(0, np.nan)
    z = (share - mean) / std
    pct = _rolling_pctile_panel(share, window)
    return {
        f"crowding_pctile_{window}d": pct,
        f"crowding_z_{window}d": z,
        "crowding_acceleration_20d": pct.diff(ACCEL_WINDOW),
    }


def fundamental_view_panel(
    financials: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    factor: str = "profit_yoy",
) -> pd.DataFrame:
    """最近披露盈利增速的截面分位面板（PIT）。

    复用 value_growth.pit_panels 拿到"披露可用日起生效、前向填充"的
    date×symbol 值面板，再对每个截面取百分位（0~1）。缺失值为 NaN
    （无财报覆盖），不参与拥挤-基本面 gap。
    """
    from quart.research.value_growth import pit_panels

    panel = pit_panels(financials, closes, factors=(factor,))[factor]
    return panel.rank(axis=1, pct=True).astype("float32")


def bad_crowding_gap(
    crowding_pctile: pd.DataFrame, fundamental_pctile: pd.DataFrame
) -> pd.DataFrame:
    """坏拥挤 gap = 拥挤分位 − 基本面分位。>0 表示"交易热度超越基本面"。"""
    gap = crowding_pctile - fundamental_pctile
    # 无基本面覆盖的股票 gap 为 NaN，不进入预警（无法判定好坏）
    return gap.where(fundamental_pctile.notna())


def rolling_adaptive_threshold(
    panel: pd.DataFrame, *, window: int = THRESHOLD_WINDOW, quantile: float = THRESHOLD_QUANTILE
) -> pd.DataFrame:
    """滚动 window 天、quantile 分位的自适应阈值（C 实现滚动分位，速度快）。"""
    return panel.rolling(window, min_periods=min(120, window // 3)).quantile(quantile)


def crowding_trigger(
    crowding_pctile: pd.DataFrame,
    *,
    threshold_window: int = THRESHOLD_WINDOW,
    threshold_quantile: float = THRESHOLD_QUANTILE,
    accel_window: int = ACCEL_WINDOW,
) -> pd.DataFrame:
    """预警事件面板：每日"首次突破自适应阈值且加速度为正"的股票记为 1。

    首次突破（此前连续处于阈值下）避免同一段拥挤期重复触发。
    """
    th = rolling_adaptive_threshold(
        crowding_pctile, window=threshold_window, quantile=threshold_quantile
    )
    above = crowding_pctile > th
    prev_above = above.shift(1, fill_value=False)
    accel = crowding_pctile.diff(accel_window).gt(0)
    first_break = above & ~prev_above
    return (first_break & accel).astype("int8")


__all__ = [
    "amount_share",
    "crowding_indicators",
    "fundamental_view_panel",
    "bad_crowding_gap",
    "rolling_adaptive_threshold",
    "crowding_trigger",
    "CROWDING_WINDOW",
    "ACCEL_WINDOW",
    "THRESHOLD_WINDOW",
    "THRESHOLD_QUANTILE",
]