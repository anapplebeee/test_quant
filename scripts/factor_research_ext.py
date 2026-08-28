"""扩展因子研究：量价扩展因子 + 财务/估值因子（2026-08-28 新增）。

量价扩展（本地 bar 数据派生）：
  rsi14_neg / atr20_neg / volume_ratio20 / amount_accel5_60
  / boll_pos20 / skew20_neg / mom12_1

财务/估值（data/factors/financials.parquet，季频，披露时滞 +120 交易日对齐）：
  roe / gross_margin / rev_yoy / profit_yoy / ep(earnings yield) / bp(book yield)

用法：python scripts/factor_research_ext.py [--sample monthly|weekly]
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

from quart.backtest.engine import MarketData
from quart.config import PROJECT_ROOT, load_config
from quart.data.store import BarStore
from scripts.factor_research import every_nth, monthly_ends

console = Console()
HORIZON = 5
DISCLOSE_LAG_DAYS = 120  # 财报披露时滞：报告期 + 120 交易日视为可用


def build_price_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    c = md.close_val
    o = md.opens
    h = md.highs
    l = md.lows
    v = md.volumes
    a = md.amounts.ffill()
    ret1 = c.pct_change(fill_method=None)

    # RSI(14)
    up = ret1.clip(lower=0).rolling(14).mean()
    dn = (-ret1.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    # ATR(20) 归一化（注意：pd.concat(axis=1) 对相同列名是堆叠不合并，TR 用 np.maximum）
    tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    atr20 = tr.rolling(20).mean() / c.shift(1).replace(0, np.nan)
    # 布林带位置
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    boll_pos = (c - ma20) / (2 * sd20.replace(0, np.nan))
    # 收益率偏度（彩票因子）
    skew20 = ret1.rolling(20).skew()
    # 12-1 月动量（跳过最近 1 个月）
    mom12_1 = c.shift(21) / c.shift(252) - 1.0

    factors = {
        "rsi14_neg": -rsi,
        "atr20_neg": -atr20,
        "volume_ratio20": v.rolling(20).mean() / v.rolling(120).mean().replace(0, np.nan),
        "amount_accel5_60": a.rolling(5).mean() / a.rolling(60).mean().replace(0, np.nan),
        "boll_pos20": boll_pos,
        "skew20_neg": -skew20,
        "mom12_1": mom12_1,
    }
    return {k: val.astype("float64") for k, val in factors.items()}


def build_fundamental_factors(md: MarketData) -> dict[str, pd.DataFrame]:
    """财务因子对齐到日频：报告期 + DISCLOSE_LAG_DAYS 起可用，ffill。"""
    path = PROJECT_ROOT / "data" / "factors" / "financials.parquet"
    if not path.exists():
        console.print("[yellow]financials.parquet 不存在，跳过财务因子[/yellow]")
        return {}
    fin = pd.read_parquet(path)
    fin["date"] = pd.to_datetime(fin["date"])

    dates = md.dates
    closes = md.close_val
    out: dict[str, pd.DataFrame] = {}
    for col, label in [("eps", "ep"), ("bps", "bp")]:
        raw = fin.pivot_table(index="date", columns="symbol", values=col, aggfunc="last")
        available = raw.index + pd.Timedelta(days=DISCLOSE_LAG_DAYS)
        raw_aligned = raw.set_axis(available)
        f = raw_aligned.reindex(dates).ffill()
        if label == "ep":
            f = f / closes  # earnings yield = eps / price
        elif label == "bp":
            f = f / closes  # book-to-price = bps / price
        out[label] = f
    for col, label in [("roe", "roe"), ("gross_margin", "gross_margin"),
                       ("rev_yoy", "rev_yoy"), ("profit_yoy", "profit_yoy")]:
        raw = fin.pivot_table(index="date", columns="symbol", values=col, aggfunc="last")
        available = raw.index + pd.Timedelta(days=DISCLOSE_LAG_DAYS)
        raw_aligned = raw.set_axis(available)
        out[label] = raw_aligned.reindex(dates).ffill()
    return {k: v.astype("float64") for k, v in out.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="monthly", choices=["monthly", "weekly"])
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(include_index=False)
    md = MarketData.from_bars(bars)

    factors = {}
    factors.update(build_price_factors(md))
    factors.update(build_fundamental_factors(md))

    label = md.close_val.shift(-(HORIZON + 1)) / md.close_val.shift(-1) - 1.0
    amed = md.amounts.ffill().rolling(20).mean()
    eligible_base = amed > 20_000_000

    bench_close = store.load_benchmark(cfg["benchmark"]).set_index("date")["close"].reindex(md.dates).ffill()
    bench_fwd = bench_close.shift(-(HORIZON + 1)) / bench_close.shift(-1) - 1.0

    sampler = monthly_ends if args.sample == "monthly" else every_nth(5)
    ends = sampler(md.dates)
    console.print(f"eval window: {md.dates[ends[0]].date()} ~ {md.dates[ends[-1]].date()} | {len(ends)} points")

    rows = {}
    for name, fw in factors.items():
        ics, spreads = [], []
        for i in ends:
            elig = eligible_base.iloc[i].fillna(False)
            joined = pd.DataFrame({"f": fw.iloc[i], "y": label.iloc[i]}).loc[elig].dropna()
            if len(joined) < 100:
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
            "ic": s.mean(), "icir": s.mean() / s.std() if s.std() else np.nan,
            "pos%": (s > 0).mean(),
            "early_half_ic": s.iloc[:half].mean(), "late_half_ic": s.iloc[half:].mean(),
            "ls_bp": float(np.nanmean(spreads)) * 10000, "n": len(s),
        }

    summary = pd.DataFrame(rows).T.sort_values("icir", key=lambda x: x.abs(), ascending=False)
    table = Table(title=f"扩展因子研究 fwd{HORIZON}d RankIC")
    for col in ["factor", "IC", "ICIR", "正率", "前半段", "后半段", "多空bp", "n"]:
        table.add_column(col, justify="right")
    for name, r in summary.iterrows():
        table.add_row(
            str(name), f"{r['ic']:+.4f}", f"{r['icir']:+.2f}", f"{r['pos%']:.0%}",
            f"{r['early_half_ic']:+.4f}", f"{r['late_half_ic']:+.4f}",
            f"{r['ls_bp']:+.0f}", str(int(r['n'])),
        )
    console.print(table)
    # 落盘研究结果
    out = PROJECT_ROOT / "reports" / "factor_research_ext.csv"
    summary.to_csv(out)
    console.print(f"saved: {out}")


if __name__ == "__main__":
    main()
