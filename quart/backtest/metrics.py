from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 243  # A 股实际年均交易日（2019-2025 实测 242.7；用 252 会低估 CAGR ~3.7%）


def total_return(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    years = len(equity) / TRADING_DAYS
    end, start = float(equity.iloc[-1]), float(equity.iloc[0])
    if start <= 0 or end <= 0:
        return 0.0
    return float((end / start) ** (1.0 / years) - 1.0)


def annual_volatility(equity: pd.Series) -> float:
    rets = equity.pct_change().dropna()
    if rets.empty:
        return 0.0
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def sharpe_ratio(equity: pd.Series, rf_annual: float = 0.0) -> float:
    rets = equity.pct_change().dropna()
    if rets.empty or rets.std(ddof=1) == 0:
        return 0.0
    excess = rets.mean() - rf_annual / TRADING_DAYS
    return float(excess / rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp | None]:
    equity = equity.dropna()
    if len(equity) < 2:
        return 0.0, None
    cummax = equity.cummax()
    dd = equity / cummax - 1.0
    trough = dd.idxmin()
    return float(dd.loc[trough]), trough


def calmar_ratio(equity: pd.Series) -> float:
    mdd, _ = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return float(cagr(equity) / abs(mdd))


def win_rate(returns: pd.Series) -> float:
    rets = returns.dropna()
    if rets.empty:
        return 0.0
    return float((rets > 0).sum() / len(rets))


# 区间窗口：键名 + 交易日数（126≈半年，252≈一年）。摘要/前端/诊断共用同一套口径
WINDOWS: tuple[tuple[str, int], ...] = (("last_6m", 126), ("last_1y", 252))


def window_stats(equity: pd.Series, days: int) -> dict:
    """近 N 个交易日的区间指标：区间收益 / 区间最大回撤 / 年化波动 / 夏普。

    交易日计数（非日历日）；样本不足时 return/mdd 为 None，调用方按缺省展示。
    """
    eq = equity.dropna()
    if len(eq) < 2:
        return {"return": None, "mdd": None, "ann_vol": None, "sharpe": None, "days": 0}
    w = eq.iloc[-(days + 1):]  # 含窗口首日作为基期
    if len(w) < 2:
        return {"return": None, "mdd": None, "ann_vol": None, "sharpe": None, "days": len(w) - 1}
    rets = w.pct_change().dropna()
    mdd, _ = max_drawdown(w)
    vol = float(rets.std() * TRADING_DAYS**0.5) if len(rets) > 1 else None
    sharpe = (
        float(rets.mean() / rets.std() * TRADING_DAYS**0.5)
        if len(rets) > 1 and rets.std() > 0
        else None
    )
    # 基期为 0 会产生 inf/nan。返回 None 而不是 inf——UI 会把 inf 显示成
    # "+inf%"，比显示"-"更糟（看起来像一个巨大的正收益）。
    base = float(w.iloc[0])
    ret = float(w.iloc[-1] / base - 1.0) if base > 0 else None
    if ret is not None and not np.isfinite(ret):
        ret = None
    return {
        "return": ret,
        "mdd": mdd if mdd is None or np.isfinite(mdd) else None,
        "ann_vol": vol if vol is None or np.isfinite(vol) else None,
        "sharpe": sharpe if sharpe is None or np.isfinite(sharpe) else None,
        "days": len(w) - 1,
    }


def _bench_metrics(result: dict, name: str, bench: pd.Series) -> None:
    """写入单个基准的对比指标（支持多基准，含区间窗口）。"""
    if bench is None or len(bench) < 2:
        return
    result[f"{name}_total_return"] = total_return(bench)
    result[f"{name}_cagr"] = cagr(bench)
    result[f"{name}_excess_cagr"] = result["cagr"] - cagr(bench)
    for label, days in WINDOWS:
        ws = window_stats(bench, days)
        result[f"{name}_{label}_return"] = ws["return"]
        result[f"{name}_{label}_mdd"] = ws["mdd"]


def summarize(
    equity: pd.Series,
    benchmark: pd.Series | None = None,
    benchmark2: pd.Series | None = None,
    benchmark2_name: str = "bench2",
) -> dict:
    rets = equity.pct_change().dropna()
    mdd, trough = max_drawdown(equity)
    result = {
        "start": str(equity.index[0].date()) if len(equity) else None,
        "end": str(equity.index[-1].date()) if len(equity) else None,
        "total_return": total_return(equity),
        "cagr": cagr(equity),
        "annual_vol": annual_volatility(equity),
        "sharpe": sharpe_ratio(equity),
        "max_drawdown": mdd,
        "mdd_trough": str(trough.date()) if trough is not None else None,
        "calmar": calmar_ratio(equity),
        "daily_win_rate": win_rate(rets),
    }
    # 持仓日胜率：剔除零收益日（空仓/停牌）后的胜率。
    # daily_win_rate 把空仓日计入分母且不算赢，对含择时/空仓期的策略系统性低估（架构评审 4.5）
    invested = rets[rets != 0]
    result["invested_win_rate"] = win_rate(invested) if len(invested) else None
    # 近半年/近一年区间指标（收益+回撤），与全周期键并存
    for label, days in WINDOWS:
        ws = window_stats(equity, days)
        result[f"{label}_return"] = ws["return"]
        result[f"{label}_mdd"] = ws["mdd"]
    _bench_metrics(result, "bench", benchmark)
    if benchmark2 is not None:
        _bench_metrics(result, benchmark2_name, benchmark2)
    return result


def format_summary(summary: dict) -> str:
    pct = lambda v: f"{v * 100:.2f}%"
    lines = [
        f"区间          {summary['start']} ~ {summary['end']}",
        f"累计收益      {pct(summary['total_return'])}",
        f"年化收益      {pct(summary['cagr'])}",
        f"年化波动      {pct(summary['annual_vol'])}",
        f"夏普比率      {summary['sharpe']:.2f}",
        f"最大回撤      {pct(summary['max_drawdown'])} (谷底 {summary['mdd_trough']})",
        f"卡玛比率      {summary['calmar']:.2f}",
        f"日胜率        {pct(summary['daily_win_rate'])}",
    ]
    if "bench_total_return" in summary:
        lines += [
            f"基准收益      {pct(summary['bench_total_return'])}",
            f"基准年化      {pct(summary['bench_cagr'])}",
            f"超额年化      {pct(summary['bench_excess_cagr'])}",
        ]
    if "bench2_total_return" in summary:
        lines += [
            f"等权基准收益  {pct(summary['bench2_total_return'])}",
            f"等权基准年化  {pct(summary['bench2_cagr'])}",
            f"对等权超额    {pct(summary['bench2_excess_cagr'])}",
        ]
    return "\n".join(lines)
