from __future__ import annotations

import argparse
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import MarketData
from quart.config import load_config
from quart.data.store import BarStore

console = Console()

HORIZON = 5


def build_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    c = md.close_val
    o = md.opens
    h = md.highs
    l = md.lows
    v = md.volumes
    a = md.amounts.ffill()
    ret1 = c.pct_change(fill_method=None)
    vwap = (a / v.replace(0, np.nan)).ffill()

    mom60 = c.pct_change(60, fill_method=None)
    factors = {
        "mom60": mom60,
        "mom120": c.pct_change(120, fill_method=None),
        "sharpe_mom60": mom60 / ret1.rolling(60).std(),
        "rev5": -c.pct_change(5, fill_method=None),
        "high_lag250": c / c.rolling(250).max() - 1.0,
        "vol20_neg": -ret1.rolling(20).std(),
        "downvol_ratio_neg": -(ret1.clip(upper=0).rolling(20).std() / ret1.abs().rolling(20).std().replace(0, np.nan)),
        "amp20_neg": -(((h - l) / c.shift(1).replace(0, np.nan)).rolling(20).mean()),
        "amp_expand20": a.rolling(20).mean() / a.rolling(120).mean(),
        "net_flow20": (np.sign(ret1) * v).rolling(20).sum() / v.rolling(20).sum(),
        "vwap_dev20": c / vwap.rolling(20).mean() - 1.0,
        "pv_corr20_neg": -ret1.rolling(20).corr(np.log(a.where(a > 0))),
        "trend_eff_dir": mom60.abs() / ret1.abs().rolling(60).sum().replace(0, np.nan) * np.sign(mom60),
        "lottery20_neg": -ret1.rolling(20).max(),
        "gap_avg": (o / c.shift(1) - 1.0).rolling(20).mean(),
    }
    return {k: val.astype("float64") for k, val in factors.items()}


def monthly_ends(dates: pd.DatetimeIndex, warmup: int = 260, tail_need: int = HORIZON + 1) -> list[int]:
    idx = pd.Series(range(len(dates)), index=dates)
    last_of_month = idx.groupby([dates.year, dates.month]).last().sort_values()
    return [int(k) for k in last_of_month if k >= warmup and k + tail_need < len(dates)]


def every_nth(days: int, warmup: int = 260, tail_need: int = HORIZON + 1):
    def _f(dates: pd.DatetimeIndex) -> list[int]:
        n = len(dates)
        return [i for i in range(warmup, n - tail_need, days)]
    return _f


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="monthly", choices=["monthly", "weekly"])
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(include_index=False)
    md = MarketData.from_bars(bars)

    factors = build_factors(md)
    label = md.close_val.shift(-(HORIZON + 1)) / md.close_val.shift(-1) - 1.0
    amed = md.amounts.ffill().rolling(20).mean()
    eligible_base = amed > 20_000_000

    bench_close = (
        store.load_benchmark(cfg["benchmark"]).set_index("date")["close"].reindex(md.dates).ffill()
    )
    bench_fwd = bench_close.shift(-(HORIZON + 1)) / bench_close.shift(-1) - 1.0

    sampler = monthly_ends if args.sample == "monthly" else every_nth(5)
    ends = sampler(md.dates)
    console.print(f"eval window: {md.dates[ends[0]].date()} ~ {md.dates[ends[-1]].date()} | {len(ends)} month-ends")

    rows = {}
    for name, fw in factors.items():
        ics, spreads = [], []
        for i in ends:
            elig = eligible_base.iloc[i].fillna(False)
            joined = pd.DataFrame({"f": fw.iloc[i], "y": label.iloc[i]}).loc[elig].dropna()
            if len(joined) < 300:
                continue
            fx, fy = joined["f"], joined["y"]
            ics.append(float(fx.corr(fy, method="spearman")))
            q_hi, q_lo = fx.quantile(0.9), fx.quantile(0.1)
            hi_y = fy[fx >= q_hi].clip(-0.5, 2.0).mean()
            lo_y = fy[fx <= q_lo].clip(-0.5, 2.0).mean()
            spread = hi_y - lo_y
            br = bench_fwd.iloc[i]
            spreads.append(float(spread - br) if not np.isnan(br) else float(spread))

        s = pd.Series(ics)
        half = max(len(s) // 2, 1)
        rows[name] = {
            "ic": s.mean(),
            "icir": s.mean() / s.std() if s.std() else np.nan,
            "pos%": (s > 0).mean(),
            "early_half_ic": s.iloc[:half].mean(),
            "late_half_ic": s.iloc[half:].mean(),
            "ls_bp": float(np.nanmean(spreads)) * 10000,
            "n": len(s),
        }

    summary = pd.DataFrame(rows).T.sort_values("icir", key=lambda x: x.abs(), ascending=False)
    table = Table(title=f"单因子研究 fwd{HORIZON}d RankIC 月频")
    for col in ["factor", "IC", "ICIR", "正率", "前半段", "后半段", "多空bp"]:
        table.add_column(col, justify="right")
    for name, r in summary.iterrows():
        table.add_row(
            str(name),
            f"{r['ic']:+.4f}",
            f"{r['icir']:+.2f}",
            f"{r['pos%']:.0%}",
            f"{r['early_half_ic']:+.4f}",
            f"{r['late_half_ic']:+.4f}",
            f"{r['ls_bp']:+.0f}",
        )
    console.print(table)


if __name__ == "__main__":
    main()
