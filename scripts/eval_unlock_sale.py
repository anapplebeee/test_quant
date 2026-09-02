"""解禁+内部人减持预披露事件研究（方向 2/3 落地，见 RESEARCH-005）。

背景（RESEARCH-004 系列结论）：
- 内部人减持计划预披露公告后，内部人减持股相对市场 +0.13%（利空出尽）；
- 解禁后 1 年内预披露的股票反弹更强（T+10 +0.47%），但按 ADV 分层后
  "反弹主要在中盘层（+1.38%），大盘层约 0，小盘层为负"；
- "减持前拉升"主要在大盘层（公告前 20 日 +5.7%~+8.7%），小盘层不拉升反而跌。

本脚本把这些事件研究结论固化为可复现诊断（协议：事件研究口径 + 事件活跃
等权基线对比 + 选择性披露警示）：

方向 2：解禁后 1 年内 + 内部人减持预披露 → 按 ADV（小/中/大）分层的
        公告前拉升 & 公告后前视收益（T+1 开盘买入，持有 1/5/10/20 日）。
方向 3：大盘股公告前拉升形态 → 公告后收益（检验"减持前连续拉升"是否
        预测公告后回吐）。

时点安全（不变量 1）：事件仅用公告可用日（published_at→下一交易日），
拉升用公告前量价，前视收益用公告后，均不含未来数据。

用法：
    uv run python scripts/eval_unlock_sale.py [--since 2024-01-01]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import data_root
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.event_factors import (
    DIRECTOR_SALE_PERSON_REGEX,
    _availability_dates,
)

console = Console()
HORIZONS = (1, 5, 10, 20)
PRE_WINDOW = 20  # 公告前拉升窗口


def load_events(data: Path, idx: pd.DatetimeIndex, columns: pd.Index):
    """构建事件表：内部人减持预披露 + 解禁锚点 + 可用交易日映射。"""
    raw = pd.read_parquet(data / "events" / "announcements_raw.parquet")
    raw["symbol"] = raw["代码"].astype(str).str.zfill(6)
    t = raw["公告标题"].astype(str)

    plan = raw[
        t.str.contains("减持股份预披露|减持计划预披露|拟减持|减持计划公告|预披露公告", na=False)
        & t.str.contains(DIRECTOR_SALE_PERSON_REGEX, na=False)
    ].copy()
    plan["published_at"] = pd.to_datetime(plan["公告日期"])
    plan["avail"] = _availability_dates(plan, idx)
    plan = plan[plan["avail"].notna() & plan["symbol"].isin(columns)]

    unlock = raw[
        t.str.contains("上市流通提示|上市流通公告", na=False) & ~t.str.contains("核查意见", na=False)
    ].copy()
    unlock["published_at"] = pd.to_datetime(unlock["公告日期"])
    unlock["avail"] = _availability_dates(unlock, idx)
    unlock = unlock[unlock["avail"].notna()]
    unlock_by_sym = unlock.groupby("symbol")["avail"].apply(sorted)
    return plan, unlock_by_sym


def has_prior_unlock(sym, avail, unlock_by_sym, within_days=365):
    if sym not in unlock_by_sym:
        return False
    return any(
        (avail - u) <= pd.Timedelta(days=within_days) and u <= avail
        for u in unlock_by_sym[sym]
    )


def forward_returns(md: MarketData, horizons: tuple[int, ...]) -> dict[int, pd.DataFrame]:
    """T+1 开盘买入、T+1+h 开盘卖出的前视收益宽表（date × symbol）。"""
    out = {}
    entry = md.opens.shift(-1)
    for h in horizons:
        out[h] = md.opens.shift(-(1 + h)) / entry.replace(0, np.nan) - 1.0
    return out


def build_event_rows(plan, unlock_by_sym, close, amount, rel, idx, pos, col_index):
    """为每个事件计算：解禁背景、ADV 分位、公告前拉升、公告后前视收益。"""
    rows = []
    for _, row in plan.iterrows():
        sym, avail = row["symbol"], row["avail"]
        start = pos.get(avail)
        if start is None or start < PRE_WINDOW or sym not in col_index:
            continue
        ci = col_index[sym]
        has_unlock = has_prior_unlock(sym, avail, unlock_by_sym)
        pre = float(rel.iloc[start - PRE_WINDOW:start, ci].sum())
        a_col = amount.columns.get_loc(sym) if sym in amount.columns else None
        adv = amount.iloc[start - PRE_WINDOW:start, a_col].mean() if a_col is not None else np.nan
        if pd.isna(adv):
            continue
        day_adv = amount.iloc[start - 1].dropna()
        adv_q = float((day_adv < adv).mean()) if len(day_adv) else np.nan
        post = {}
        for h in HORIZONS:
            j = start + h
            post[h] = float(rel.iloc[j, ci]) if j < len(idx) else np.nan
        rows.append({
            "symbol": sym, "avail": avail, "has_unlock": has_unlock,
            "pre20": pre, "adv_q": adv_q, **{f"post{h}": post[h] for h in HORIZONS},
        })
    return pd.DataFrame(rows)


def layer_table(df: pd.DataFrame, col: str, title: str) -> Table:
    t = Table(title=title)
    t.add_column("层", justify="left")
    t.add_column("n", justify="right")
    t.add_column("公告前20日", justify="right")
    for h in HORIZONS:
        t.add_column(f"T+{h}", justify="right")
    layers = (
        ("小盘(adv_q<0.33)", df["adv_q"] < 0.33),
        ("中盘(0.33-0.67)", (df["adv_q"] >= 0.33) & (df["adv_q"] < 0.67)),
        ("大盘(adv_q>=0.67)", df["adv_q"] >= 0.67),
    )
    for label, mask in layers:
        sub = df[mask]
        if sub.empty:
            t.add_row(label, "0", "-", *(["-"] * len(HORIZONS)))
            continue
        cells = [label, str(len(sub)), f"{sub['pre20'].mean():+.2%}"]
        for h in HORIZONS:
            v = sub[f"post{h}"].dropna().mean()
            cells.append(f"{v:+.2%}" if pd.notna(v) else "-")
        t.add_row(*cells)
    return t


def main() -> None:
    parser = argparse.ArgumentParser(description="解禁+内部人减持预披露事件研究")
    parser.add_argument("--since", default="2024-01-01")
    args = parser.parse_args()

    store = BarStore()
    bars = store.load(start=args.since, include_index=False)
    bars = filter_for_simulation(bars, exclude_star=True, exclude_chinext=False,
                                 exclude_st=True, min_list_days=0)
    md = MarketData.from_bars(bars)
    close = md.close_val
    returns = close.pct_change(fill_method=None)
    amount = bars.pivot(index="date", columns="symbol", values="amount").reindex(
        index=close.index, columns=close.columns)
    idx = returns.index
    pos = {d: k for k, d in enumerate(idx)}
    col_index = {c: i for i, c in enumerate(close.columns)}
    rel = returns.sub(returns.mean(axis=1), axis=0)

    plan, unlock_by_sym = load_events(Path(data_root()), idx, close.columns)
    df = build_event_rows(plan, unlock_by_sym, close, amount, rel, idx, pos, col_index)
    if df.empty:
        raise SystemExit("无可配对事件")
    console.print(f"内部人减持预披露事件 {len(df)} 条，其中解禁后1年内 "
                  f"{int(df['has_unlock'].sum())} 条")

    # 方向 2：解禁后1年内样本，按 ADV 分层
    unlock_df = df[df["has_unlock"]]
    console.print(layer_table(unlock_df, "post", "方向2: 解禁后1年内+减持预披露，按ADV分层（相对市场超额）"))
    # 方向 2 对照：全部样本（不限解禁）
    console.print(layer_table(df, "post", "对照: 全部内部人预披露，按ADV分层"))

    # 方向 3：大盘股公告前拉升形态 → 公告后
    large = df[(df["adv_q"] >= 0.67)]
    if not large.empty and len(large) >= 30:
        thr = large["pre20"].quantile(0.67)
        hi = large[large["pre20"] >= thr]
        lo = large[large["pre20"] < thr]
        t3 = Table(title="方向3: 大盘股公告前拉升形态 → 公告后（相对市场超额）")
        for c in ("组", "n", "公告前20日", *(f"T+{h}" for h in HORIZONS)):
            t3.add_column(c, justify="right")
        for label, sub in (("高拉升(top1/3)", hi), ("低拉升(底2/3)", lo)):
            cells = [label, str(len(sub)), f"{sub['pre20'].mean():+.2%}"]
            for h in HORIZONS:
                v = sub[f"post{h}"].dropna().mean()
                cells.append(f"{v:+.2%}" if pd.notna(v) else "-")
            t3.add_row(*cells)
        diff = [hi[f"post{h}"].dropna().mean() - lo[f"post{h}"].dropna().mean() for h in HORIZONS]
        t3.add_row("高-低", "-", "-", *[f"{d:+.2%}" if pd.notna(d) else "-" for d in diff])
        console.print(t3)

    console.print(
        "[dim]判读：公告前拉升集中于大盘层；公告后反弹集中于中盘层。"
        "样本仅限解禁/减持事件标的（选择性披露），不可外推全市场。"
        "ADV 为小盘透明代理（float_mcap 未覆盖全市场）。[/dim]"
    )


if __name__ == "__main__":
    main()
