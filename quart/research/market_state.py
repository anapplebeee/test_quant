"""市场状态预测 × 动态因子路由（RESEARCH-003 方向一）。

背景
----
RESEARCH-002 的核心失败教训是"正交混入低换手组合反而劣化"，根因不是因子
无效而是持仓期与组合节奏不匹配。方向一的解法不是把短周期信号硬塞进长周期
组合，而是让市场状态决定"当前该信任哪套因子"。

本模块提供：
- ``market_state_vector``：把每个交易日离散化为 risk_on / transition / risk_off
  三种状态。输入复用 §5.2 已有市场时序信号（涨跌停广度/热度 z）+ 市场换手
  热度 z + 基准波动率百分位；输出带最小持续天数的去抖状态（避免频繁切换）。
- ``state_conditional_ic``：对一组日期×符号因子面板，按状态分层计算 RankIC，
  检验"同一因子在不同市场状态下有效性不同"这一路由前提。

OOS 合规（§8）
---------------
状态判定规则与去抖参数属于研究假设；任何阈值调整都不允许在 2023~2026 段
进行并称 OOS。评估脚本默认输出 2023-01 前（学习段）与 2023-01 后（单一诊断
段，仅观察一次）两段结果。
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

#: 状态名
RISK_ON = "risk_on"
TRANSITION = "transition"
RISK_OFF = "risk_off"


def _zscore(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window, min_periods=max(5, window // 3)).mean()
    std = s.rolling(window, min_periods=max(5, window // 3)).std().replace(0, np.nan)
    return (s - mean) / std


def _rolling_pctile(s: pd.Series, window: int) -> pd.Series:
    """滚动经验百分位（native 循环在长度 ~1500 的市场序列上可接受）。"""
    arr = s.to_numpy(dtype="float64")
    out = np.full(len(arr), np.nan)
    start = min(window, 1)
    for i in range(max(0, window - 1), len(arr)):
        win = arr[max(0, i - window + 1): i + 1]
        win = win[~np.isnan(win)]
        out[i] = (arr[i] >= win).mean() if win.size else np.nan
    return pd.Series(out, index=s.index)


def market_state_vector(
    signals: pd.DataFrame,
    *,
    bench_close: pd.Series | None = None,
    breadth_z_window: int = 60,
    turnover_z_window: int = 20,
    vol_window: int = 20,
    vol_pct_window: int = 250,
    min_days: int = 5,
    composite_window: int = 60,
    upper_quantile: float = 0.66,
    lower_quantile: float = 0.33,
) -> pd.DataFrame:
    """把每日市场状态离散化为 risk_on / transition / risk_off。

    Args:
        signals: 需含 ``limit_heat_z``（涨跌停净广度 z，可由
            ``market_limit_sentiment`` 产出）与 ``amount``（全市场成交额）。
        bench_close: 基准收盘价（可传 md.benchmark_close），用于计算波动率；
            为 None 时波动率项为空，状态只由热度/换手决定。
        min_days: 状态最短持续天数，不足的片段并入 transition（去抖）。

    状态规则（研究假设，非调参结果）：先合成市场状态分
        score = 0.4·heat_z + 0.3·amount_z − 0.3·(波动率 z)，再取其滚动
        ``composite_window`` 的经验百分位：>= 0.66 → risk_on，<= 0.33 →
        risk_off，其余 transition。三分位保证状态分布相对均衡，避免
        多条件取与导致的 transition 占绝大比例。
    """
    if "limit_heat_z" not in signals or "amount" not in signals:
        raise ValueError("signals 需含 limit_heat_z 与 amount 列")
    idx = pd.DatetimeIndex(signals.index)
    heat_z = signals["limit_heat_z"].astype(float)
    amount_z = _zscore(
        pd.Series(signals["amount"].astype(float).to_numpy(), index=idx), turnover_z_window
    )
    if bench_close is not None:
        ret = pd.Series(bench_close.to_numpy(), index=idx).pct_change()
        vol = ret.rolling(vol_window, min_periods=max(5, vol_window // 3)).std()
        vol_pct = pd.Series(_rolling_pctile(vol, window=vol_pct_window).to_numpy(), index=idx)
        vol_z = _zscore(vol_pct, window=vol_pct_window)
    else:
        vol_pct = pd.Series(np.nan, index=idx)
        vol_z = pd.Series(0.0, index=idx)

    parts = [0.4 * heat_z, 0.3 * amount_z, -0.3 * vol_z]
    comp = pd.concat(parts, axis=1).sum(axis=1, min_count=1)
    comp_pct = pd.Series(_rolling_pctile(comp, window=composite_window).to_numpy(), index=idx)
    raw = pd.Series(TRANSITION, index=idx)
    raw[comp_pct >= upper_quantile] = RISK_ON
    raw[comp_pct <= lower_quantile] = RISK_OFF

    # 去抖：不足 min_days 的连续状态段并入 transition
    state_arr = raw.to_numpy()
    out = state_arr.copy()
    i = 0
    n = len(state_arr)
    while i < n:
        j = i
        while j < n and state_arr[j] == state_arr[i]:
            j += 1
        if state_arr[i] != TRANSITION and (j - i) < min_days:
            out[i:j] = TRANSITION
        i = j

    return pd.DataFrame(
        {
            "state": out,
            "limit_heat_z": heat_z,
            "amount_z": amount_z,
            "vol_pct": vol_pct,
            "composite_pct": comp_pct,
        },
        index=idx,
    )


def state_conditional_ic(
    factors: dict[str, pd.DataFrame],
    md,
    states: pd.DataFrame,
    starts: Iterable[int],
    *,
    horizon: int = 5,
    min_symbols: int = 300,
) -> pd.DataFrame:
    """对每个因子按市场状态分层计算 RankIC。

    逻辑与 scripts/mine_factors.py 的 evaluate_factors 同口径（同一 label、
    同一 eligible 过滤），只是把 IC 按当日状态分组统计，输出
    global / risk_on / transition / risk_off 四列 IC 与样本天数。

    Returns:
        DataFrame(index=factor): 列 global_ic, global_n, risk_on_ic, risk_on_n,
        transition_ic, transition_n, risk_off_ic, risk_off_n, ic_gap(risk_on-risk_off)
    """
    frame = md.opens
    label = frame.shift(-(horizon + 1)) / frame.shift(-1).replace(0, np.nan) - 1.0
    amed = md.amounts.rolling(20).mean()
    eligible = amed > 20_000_000
    state_by_date = states["state"]

    from quart.research.factor_audit import rank_correlation

    state_cols = ["global", RISK_ON, TRANSITION, RISK_OFF]
    rows: dict[str, dict[str, float]] = {}
    for name, fw in factors.items():
        fw = fw.copy()
        fw.columns = [str(symbol).replace(".0", "").zfill(6) for symbol in fw.columns]
        fw = fw.reindex(index=md.dates, columns=md.symbols)
        acc: dict[str, list[float]] = {s: [] for s in state_cols}
        for i in starts:
            elig = eligible.iloc[i].fillna(False)
            joined = pd.DataFrame({"f": fw.iloc[i], "y": label.iloc[i]}).loc[elig].dropna()
            if len(joined) < min_symbols:
                continue
            ic = rank_correlation(joined["f"], joined["y"])
            acc["global"].append(ic)
            st = state_by_date.iloc[i]
            if st in (RISK_ON, TRANSITION, RISK_OFF):
                acc[st].append(ic)
        if not acc["global"]:
            continue
        row: dict[str, float] = {}
        for s in state_cols:
            vals = np.asarray(acc[s], dtype="float64")
            # 因子在无效期内为常数（如事件因子在事件数据起始前全 0），
            # rank_correlation 返回 nan，不计入统计（nan 感知均值）
            vals = vals[~np.isnan(vals)]
            row[f"{s}_ic"] = float(vals.mean()) if vals.size else np.nan
            row[f"{s}_n"] = int(vals.size)
        row["ic_gap"] = (
            row[f"{RISK_ON}_ic"] - row[f"{RISK_OFF}_ic"]
            if (row[f"{RISK_ON}_n"] and row[f"{RISK_OFF}_n"]
                and not np.isnan(row[f"{RISK_ON}_ic"])
                and not np.isnan(row[f"{RISK_OFF}_ic"]))
            else np.nan
        )
        rows[name] = row
    df = pd.DataFrame(rows).T
    if not df.empty:
        df = df.sort_values("ic_gap", key=lambda x: x.abs(), ascending=False)
    return df


__all__ = ["market_state_vector", "state_conditional_ic", "RISK_ON", "TRANSITION", "RISK_OFF"]