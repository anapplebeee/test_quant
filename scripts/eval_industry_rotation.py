"""RESEARCH-003 方向三评估：行业动量-反转状态切换。

用法:
    uv run python scripts/eval_industry_rotation.py

基于申万一级行业指数日线（data/industry/sw_industry_daily.parquet）：
- 变盘指数 = 行业日收益截面离散度的 20 日均值，对其滚动 z 的一阶差分：
  <0 => 主线稳定 => 动量模式（选过去 lookback 日涨幅前 3 行业）
  >0 => 主线重估 => 反转模式（选过去 5 日跌幅前 3 行业）
- 周频（每 5 个交易日）调仓，T+1 开盘成交（PIT 无未来函数）。
- 对比：恒定动量（择券并保留切换逻辑下的参照组）+ 恒定反转 + 切换策略，
  检验"切换是否优于恒定规则"；再做 lookback/平滑参数敏感性。

OOS 合规：全部参数是研究假设，学习段（<=2022-12）标定检查，2023+ 为单次诊断。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from quart.config import data_root

console = Console()

REBAL = 5  # 周频调仓
MOM_LOOKBACK = 60
REV_LOOKBACK = 5
SMOOTH = 20
START = "2015-01-01"
SPLIT = "2023-01-01"


def load_industry_closes(path: Path) -> pd.DataFrame:
    raw = pd.read_parquet(path)
    wide = raw.pivot(index="日期", columns="industry", values="收盘")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index().ffill()


def regime_signal(closes: pd.DataFrame, *, smooth: int = SMOOTH) -> pd.Series:
    """变盘指数 = 离散度 20 日均值，滚动 3 年 z 的一阶差分。

    >=0 => 主线重估/切换（反转模式）；<0 => 主线稳定（动量模式）。
    """
    ret = closes.pct_change()
    valid = ret.notna().sum(axis=1)
    disp = ret.std(axis=1).where(valid >= 15).rolling(smooth, min_periods=5).mean()
    z = (disp - disp.rolling(250, min_periods=60).mean()) / disp.rolling(
        250, min_periods=60
    ).std().replace(0, np.nan)
    change = z.diff(1).fillna(0.0)
    return change


def select_industries(
    closes: pd.DataFrame, idx: pd.DatetimeIndex, pos: int, mode: str, *, lookback: int = MOM_LOOKBACK
) -> pd.Index:
    """在 idx[pos] 收盘观测、T+1 开盘执行；返回选中的行业名称列表。"""
    date = idx[pos]
    cur = closes.loc[date].dropna()
    lb = REV_LOOKBACK if mode == "reversal" else lookback
    prev = closes.shift(lb).loc[date].reindex(cur.index)
    win = (cur / prev - 1.0).dropna()
    if mode == "momentum":
        ranked = win.sort_values(ascending=False)
    else:  # reversal
        ranked = win.sort_values(ascending=True)
    return ranked.head(3).index


def backtest_switch(
    closes: pd.DataFrame, *, mode: str = "switch", lookback: int = MOM_LOOKBACK,
    smooth: int = SMOOTH, split: str = SPLIT,
) -> pd.DataFrame:
    """mode: 'switch' | 'momentum' | 'reversal' 对照。返回逐期收益长表。"""
    idx = closes.index
    start_i = idx.searchsorted(pd.Timestamp(START))
    horizon = REBAL
    regime = regime_signal(closes, smooth=smooth)
    rows: list[dict] = []
    for pos in range(start_i, len(idx) - horizon - 1, REBAL):
        date = idx[pos]
        if mode == "switch":
            m = "momentum" if regime.loc[date] < 0 else "reversal"
        else:
            m = mode
        picks = select_industries(closes, idx, pos, m, lookback=lookback)
        entry = closes.loc[idx[pos + 1], picks]
        exit_ = closes.loc[idx[pos + 1 + horizon], picks]
        ret = (exit_ / entry - 1.0).mean()
        bench_ret = (
            closes.loc[idx[pos + 1 + horizon]] / closes.loc[idx[pos + 1]] - 1.0
        ).dropna().mean()
        rows.append(
            {
                "date": date,
                "mode": m,
                "ret": ret,
                "bench": bench_ret,
                "excess": ret - bench_ret,
                "segment": "learn" if date < pd.Timestamp(split) else "diag",
            }
        )
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, segment: str) -> dict:
    d = df[df["segment"] == segment]
    if len(d) < 5:
        return {"segment": segment, "n": int(len(d))}
    ann = 52
    mean_r = d["ret"].mean()
    std_r = d["ret"].std()
    return {
        "segment": segment,
        "n": int(len(d)),
        "cagr_pct": ((1 + mean_r) ** ann - 1) * 100,
        "sharpe": float(mean_r / std_r * np.sqrt(ann)) if std_r else np.nan,
        "excess_ann_pct": ((1 + d["excess"].mean()) ** ann - 1) * 100,
        "hit_rate": float((d["excess"] > 0).mean()),
    }


def main() -> None:
    path = Path(data_root()) / "industry" / "sw_industry_daily.parquet"
    if not path.exists():
        raise SystemExit("缺少申万行业数据，请先运行抓取（见 RESEARCH_003 §4 数据源）")
    closes = load_industry_closes(path)

    modes = {
        "switch": {"mode": "switch"},
        "momentum": {"mode": "momentum"},
        "reversal": {"mode": "reversal"},
    }
    console.print(f"[bold]行业轮动回测[/bold] {START}~2026-08，周频（{REBAL} 日），"
                  f"动量回望 {MOM_LOOKBACK}d / 反转 {REV_LOOKBACK}d / 平滑 {SMOOTH}d")
    table = Table(title="策略 vs 恒定规则的年度化表现（超额 = 相对当日等权行业）")
    for col in ["mode", "segment", "n", "cagr%", "sharpe", "excess_ann%", "hit%"]:
        table.add_column(col, justify="right")
    rows_all: dict[str, pd.DataFrame] = {}
    for name, kw in modes.items():
        df = backtest_switch(closes, **kw)
        rows_all[name] = df
        for seg in ("learn", "diag"):
            s = summarize(df, seg)
            table.add_row(
                name, seg, str(s.get("n", "-")),
                f"{s.get('cagr_pct', float('nan')):.1f}",
                f"{s.get('sharpe', float('nan')):.2f}",
                f"{s.get('excess_ann_pct', float('nan')):.1f}",
                f"{s.get('hit_rate', float('nan')):.0%}",
            )
    console.print(table)
    console.print(
        "判读：if switch 超额显著优于恒定动量/反转 => 状态切换有价值；"
        "学习段为检视、诊断段单次观察。"
    )

    # ---- 参数敏感性（只对 switch） ----
    console.print("\n[bold]切换策略参数敏感性（lookback × 平滑）[/bold]")
    st = Table()
    for col in ["lookback", "smooth", "learn_excess_ann%", "diag_excess_ann%", "diag_sharpe"]:
        st.add_column(col, justify="right")
    for lb in (120, 180, 220, 300):
        for sm in (10, 20, 40):
            df = backtest_switch(closes, mode="switch", lookback=lb, smooth=sm)
            l = summarize(df, "learn")
            d = summarize(df, "diag")
            st.add_row(
                str(lb), str(sm),
                f"{l.get('excess_ann_pct', float('nan')):.1f}",
                f"{d.get('excess_ann_pct', float('nan')):.1f}",
                f"{d.get('sharpe', float('nan')):.2f}",
            )
    console.print(st)


if __name__ == "__main__":
    logger.remove()
    main()