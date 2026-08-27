from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from rich.console import Console
from sklearn.cluster import KMeans

from quart.config import PROJECT_ROOT
from quart.data.store import BarStore

console = Console()

OUT_PATH = PROJECT_ROOT / "data" / "universe" / "stat_industry.parquet"


def build_clusters(lookback: int, min_history: int, min_avg_amount: float, n_clusters: int) -> pd.DataFrame:
    store = BarStore()
    bars = store.load(include_index=False)
    dates = sorted(bars["date"].unique())
    cutoff_idx = max(len(dates) - lookback, 0)
    window = bars[bars["date"] >= dates[cutoff_idx]]

    close = window.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    amount = window.pivot_table(index="date", columns="symbol", values="amount", aggfunc="last").sort_index()

    coverage = close.notna().sum()
    avg_amt = amount.mean()
    keep = (coverage >= min_history) & (avg_amt >= min_avg_amount)
    close = close.loc[:, keep]

    rets = np.log(close).diff().iloc[1:]
    valid_counts = rets.notna().sum()
    rets = rets.loc[:, valid_counts >= int(lookback * 0.7)]
    symbols = list(rets.columns)
    console.print(f"clustering {len(symbols)} liquid symbols over {rets.shape[0]} return days")

    r = rets.to_numpy(dtype=np.float64)
    col_mean = np.nanmean(r, axis=0)
    col_std = np.nanstd(r, axis=0)
    col_std[col_std == 0] = 1.0
    z = (r - col_mean) / col_std
    z = np.nan_to_num(z, nan=0.0)

    corr = None
    model = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    labels = model.fit_predict(np.ascontiguousarray(z.T, dtype=np.float32))
    assert len(labels) == len(symbols), f"label count {len(labels)} != symbols {len(symbols)}"

    sizes = pd.Series(labels).value_counts()
    console.print(f"clusters: {len(sizes)} | size median={sizes.median():.0f} max={sizes.max()}")

    series = pd.Series({sym: f"S{lab:03d}" for sym, lab in zip(symbols, labels)}, name="cluster")
    df = series.rename_axis("symbol").reset_index()
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Build statistical industry clusters from return correlations")
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--min-history", type=int, default=80)
    parser.add_argument("--min-avg-amount", type=float, default=20_000_000)
    parser.add_argument("--clusters", type=int, default=40)
    args = parser.parse_args()

    df = build_clusters(args.lookback, args.min_history, args.min_avg_amount, args.clusters)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    console.print(f"[green]saved[/green] {len(df)} symbols -> {OUT_PATH}")


if __name__ == "__main__":
    main()
