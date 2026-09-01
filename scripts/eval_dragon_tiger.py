"""龙虎榜事件后 1/3/5/10/20 日衰减与反转检验（RESEARCH-002 §8-3）。

方法：
- 事件研究口径：以每条上榜记录为事件，T+1 开盘买入、T+1+h 开盘卖出的
  前视收益（与 mine_factors 的 5 日标签同口径）；
- 横截面分组：按事件日 ``净买额/榜单成交额`` 五分位，输出各 horizon 的
  Q1-Q5 均值收益与多空价差 —— 观察延续（Q5>Q1）还是反转（Q5<Q1）；
- 分席位：机构净买比例 vs 营业部/游资净买比例分别做同一检验。

选择性披露警示：分位数只在"上榜样本"内比较，不代表全市场。

用法：
    uv run python scripts/eval_dragon_tiger.py --since 2023-01-01
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

from quart.config import data_root, load_config
from quart.data.market import MarketData
from quart.data.store import BarStore

console = Console()
HORIZONS = (1, 3, 5, 10, 20)


def forward_returns(md: MarketData, horizons: tuple[int, ...]) -> dict[int, pd.DataFrame]:
    """T+1 开盘买入、T+1+h 开盘卖出的前视收益宽表（date × symbol）。"""
    out = {}
    entry = md.opens.shift(-1)
    for h in horizons:
        exit_px = md.opens.shift(-(1 + h))
        out[h] = exit_px / entry.replace(0, np.nan) - 1.0
    return out


def bucket_returns(events: pd.DataFrame, score_col: str, fwd: dict[int, pd.DataFrame],
                   q: int = 5) -> pd.DataFrame:
    """按事件日 score 五分位分组，统计各 horizon 的均值收益（bp）。"""
    ev = events.dropna(subset=[score_col]).copy()
    rows = []
    for h, fw in fwd.items():
        vals = []
        for _, r in ev.iterrows():
            d = pd.Timestamp(r["published_at"])
            if d in fw.index and r["symbol"] in fw.columns:
                v = fw.at[d, r["symbol"]]
                if pd.notna(v):
                    vals.append((d, v, float(r[score_col])))
        if len(vals) < 100:
            rows.append({"horizon": h, "n": len(vals), **{f"Q{i+1}": np.nan for i in range(q)},
                         "Q5_Q1_bp": np.nan})
            continue
        df = pd.DataFrame(vals, columns=["date", "ret", "score"])
        try:
            df["bucket"] = df.groupby("date")["score"].transform(
                lambda s: pd.qcut(s, q, labels=False, duplicates="drop")
            )
        except ValueError:
            rows.append({"horizon": h, "n": len(vals), **{f"Q{i+1}": np.nan for i in range(q)},
                         "Q5_Q1_bp": np.nan})
            continue
        means = df.groupby("bucket")["ret"].mean()
        row = {"horizon": h, "n": len(df)}
        for i in range(q):
            row[f"Q{i+1}"] = means.get(i, np.nan) * 10_000
        if pd.notna(means.get(0)) and pd.notna(means.get(q - 1)):
            row["Q5_Q1_bp"] = (means.get(q - 1) - means.get(0)) * 10_000
        else:
            row["Q5_Q1_bp"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="龙虎榜 1/3/5/10/20 日衰减与反转检验")
    parser.add_argument("--since", default="2023-01-01", help="事件起点（与抓取口径一致）")
    parser.add_argument("--q", type=int, default=5)
    args = parser.parse_args()

    path = Path(data_root()) / "events" / "dragon_tiger.parquet"
    if not path.exists():
        raise SystemExit(f"缺少 {path}，先运行 scripts/fetch_dragon_tiger.py")
    events = pd.read_parquet(path)
    events = events[pd.to_datetime(events["published_at"]) >= pd.Timestamp(args.since)]
    if events.empty:
        raise SystemExit("指定区间内无事件")

    cfg = load_config()
    store = BarStore()
    bars = store.load(start=str(pd.Timestamp(args.since).date()))
    if bars.empty:
        raise SystemExit("本地行情为空")
    md = MarketData.from_bars(bars)
    bench = store.load_benchmark(cfg["benchmark"])

    turnover = pd.to_numeric(events["turnover_amount"], errors="coerce").replace(0, np.nan)
    events["net_ratio"] = pd.to_numeric(events["net_buy_amount"], errors="coerce") / turnover
    events["inst_ratio"] = pd.to_numeric(events["institution_net_buy_amount"], errors="coerce") / turnover
    events["branch_ratio"] = pd.to_numeric(events["branch_net_buy_amount"], errors="coerce") / turnover

    fwd = forward_returns(md, HORIZONS)
    console.print(f"事件数 {len(events)} / 符号 {events['symbol'].nunique()}，"
                  f"区间 {events['published_at'].min().date()} ~ {events['published_at'].max().date()}")

    for label, col in (("全席位净买比例", "net_ratio"), ("机构席位净买比例", "inst_ratio"),
                       ("营业部/游资净买比例", "branch_ratio")):
        t = Table(title=f"龙虎榜事件研究：{label}（Q{args.q} 分位，bp）")
        for c in ("horizon", "n", *(f"Q{i+1}" for i in range(args.q)), "Q5_Q1_bp"):
            t.add_column(c, justify="right")
        for _, r in bucket_returns(events, col, fwd, args.q).iterrows():
            cells = [str(int(r["horizon"])), str(int(r["n"]))]
            cells += [f"{r[f'Q{i+1}']:.0f}" if pd.notna(r[f"Q{i+1}"]) else "-" for i in range(args.q)]
            dq = r["Q5_Q1_bp"]
            cells.append(f"{dq:+.0f}" if pd.notna(dq) else "-")
            t.add_row(*cells)
        console.print(t)

    console.print(
        "[dim]判读：Q5_Q1_bp>0 且随 horizon 衰减 = 短期延续；"
        "<0 = 反转；机构与游资符号相反 = 席位结构信息。"
        "样本仅限上榜标的（选择性披露），不可外推全市场。[/dim]"
    )


if __name__ == "__main__":
    main()
