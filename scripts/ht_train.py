"""ht_train.py — 热点/龙头 LightGBM 滚动训练正式 CLI。

从 BarStore 载入行情(默认2024起全市场)，复用 domain 研报技术因子做特征，
在"训练池(宽)"内预测未来 N 日横截面相对强弱，逐月 expanding 滚动训练，
样本外算 Rank IC。输出 IC 汇总到 reports/ht_ic_<run>.csv + meta.json。

用法示例（PowerShell）：
    .venv\\Scripts\\python.exe scripts/ht_train.py --start 2024-01-01 --end 2026-09-01 \
        --horizon 20 --min-train-bars 150 --out reports/ht_ic_tech.csv
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from quart.config import PROJECT_ROOT, load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.research.ht_train import (
    compute_feature_long,
    label_and_filter,
    rolling_ic,
)
from quart.research.ht_universe import build_pools
from rich.console import Console
from rich.table import Table

console = Console()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2099-12-31")
    p.add_argument("--horizon", type=int, default=20)
    p.add_argument("--min-train-bars", type=int, default=150)
    p.add_argument("--capital", type=float, default=30_000.0)
    p.add_argument("--out", default=None, help="IC csv path (default reports/ht_ic_*.csv)")
    args = p.parse_args()

    store = BarStore()
    bench_code = load_config()["benchmark"]
    t0 = time.time()
    console.print(f"[cyan]loading bars {args.start}..{args.end}[/cyan]")
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(bench_code)
    console.print(f"[cyan]bars {bars.shape} in {time.time()-t0:.0f}s[/cyan]")

    # 训练池(宽)
    tr, pos, st = build_pools(bars, capital=args.capital)
    console.print(f"[blue]pool: {st}[/blue]")
    train_syms = sorted(tr["symbol"].unique())

    md = MarketData.from_bars(bars[bars["symbol"].isin(train_syms)], benchmark=bench)
    console.print(f"[cyan]MarketData {md.dates[0].date()}~{md.dates[-1].date()} syms={len(md.symbols)}[/cyan]")

    t0 = time.time()
    fea = compute_feature_long(md)
    console.print(f"[cyan]features {fea.shape} in {time.time()-t0:.0f}s[/cyan]")

    t0 = time.time()
    labeled, y, syms = label_and_filter(fea, md.closes, args.horizon, tr)
    dates = labeled["date"]
    X = labeled.drop(columns=["date"])
    console.print(f"[cyan]labeled X {X.shape} in {time.time()-t0:.0f}s[/cyan]")

    t0 = time.time()
    ic_rows, _ = rolling_ic(dates, X, y, min_train_bars=args.min_train_bars)
    console.print(f"[cyan]rolling IC done ({time.time()-t0:.0f}s, {len(ic_rows)} months)[/cyan]")
    if not ic_rows:
        console.print("[red]no months trained; widen window or lower min-train-bars[/red]")
        return

    # 生成每月末再平衡日的 ML 分数（供板块内选龙头）
    from quart.research.ht_train import predict_rebalance_scores
    t0 = time.time()
    calendar = dates
    score_df, score_ic = predict_rebalance_scores(
        dates, syms, X, y, calendar, min_train_bars=args.min_train_bars)
    console.print(f"[cyan]rebalance scores {score_df.shape} in {time.time()-t0:.0f}s[/cyan]")
    if not score_df.empty:
        spath = PROJECT_ROOT / "reports" / "ht_ml_scores.csv"
        score_df.to_csv(spath, index=False)
        console.print(f"[green]scores saved {spath}[/green]")

    ic_df = pd.DataFrame(ic_rows)
    ic_mean = ic_df["ic"].mean()
    ic_std = ic_df["ic"].std()
    pos_ratio = (ic_df["ic"] > 0).mean()

    tab = Table(title=f"Hot-Leader LGBM Rank IC (horizon={args.horizon}d)")
    tab.add_column("metric")
    tab.add_column("value", justify="right")
    tab.add_row("months", str(len(ic_df)))
    tab.add_row("IC mean", f"{ic_mean:+.4f}")
    tab.add_row("IC std", f"{ic_std:.4f}")
    tab.add_row("ICIR", f"{ic_mean/ic_std:.2f}" if ic_std else "-")
    tab.add_row("positive ratio", f"{pos_ratio:.0%}")
    console.print(tab)

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "reports" / f"ht_ic_tech_{pd.Timestamp.now():%Y%m%d_%H%M}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    ic_df.to_csv(out, index=False)
    meta = {
        "ic_mean": float(ic_mean), "ic_std": float(ic_std), "icir": float(ic_mean / ic_std),
        "positive_ratio": float(pos_ratio), "months": len(ic_df),
        "horizon": args.horizon, "features": X.shape[1],
        "pool": st,
    }
    with open(out.with_suffix(".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    console.print(f"[green]saved {out}[/green]")


if __name__ == "__main__":
    main()
