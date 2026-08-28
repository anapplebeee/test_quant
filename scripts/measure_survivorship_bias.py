"""幸存者偏差量化：含/不含退市股的对照测量。

测量三层：
  1) 等权宇宙（每日再平衡）：幸存者偏差的教科书口径
  2) 等权宇宙（5 日再平衡）：与策略同频率的诚实口径
  3) 引擎级：随机 Top10 × 3 种子在含/不含退市股池中的差异（选股策略实际付出的代价）

注意：退市股无法用当前 ST 名单过滤（已不在市），filter_st 对其天然放行——
这是保守且正确的反幸存者方向。退市股名单来自 data/universe/delisted.csv。

用法：.venv/Scripts/python.exe scripts/measure_survivorship_bias.py
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation

from diag_random_decomp import RandomTopKStrategy, k_day_rebal, yearly

console = Console()


def cagr_of(curve: pd.Series) -> float:
    # 按真实时间跨度年化（兼容 5 日分段序列，勿用 len/252）
    span = (curve.index[-1] - curve.index[0]).days / 365.25
    return float(curve.iloc[-1] / curve.iloc[0]) ** (1 / span) - 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--seeds", type=int, default=3)
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars_all = store.load(start=args.start)
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[bench["date"] >= args.start]

    delisted_syms = set()
    delist_path = store.universe_dir / "delisted.csv"
    if delist_path.exists():
        dl = pd.read_csv(delist_path, dtype={"symbol": str})
        delisted_syms = set(dl["symbol"].str.zfill(6))
    backfilled = {p.stem for p in store.daily_dir.glob("*.parquet")} & delisted_syms
    console.print(f"退市名单 {len(delisted_syms)} 只，其中本地有行情: {len(backfilled)} 只")
    if not backfilled:
        raise SystemExit("本地无退市股行情，请先运行 scripts/backfill_delisted.py")

    data_cfg = cfg.get("data", {})
    fkw = dict(
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    bars_with = filter_for_simulation(bars_all, **fkw)
    bars_wo = filter_for_simulation(bars_all[~bars_all["symbol"].isin(delisted_syms)], **fkw)
    console.print(f"含退市股: {bars_with['symbol'].nunique()} 只 | 不含: {bars_wo['symbol'].nunique()} 只")

    md_with = MarketData.from_bars(bars_with, benchmark=bench)
    md_wo = MarketData.from_bars(bars_wo, benchmark=bench)

    rows = {}
    for tag, md in (("含", md_with), ("不含", md_wo)):
        rets = md.closes.pct_change(fill_method=None).iloc[1:]
        ew_daily = (1 + rets.mean(axis=1)).cumprod()
        ew_5d_seg = k_day_rebal(rets, 5)
        rows[tag] = {
            "ew_daily": cagr_of(ew_daily),
            "ew_5d": cagr_of((1 + ew_5d_seg).cumprod()),
            "n_sym": md.closes.shape[1],
        }

    # 引擎级：随机 Top10 对照
    params = dict(
        top_k=args.top_k,
        rebalance_days=5,
        max_weight_pct=0.15,
        min_avg_amount=cfg["strategy"].get("min_avg_amount"),
        liquidity_days=cfg["strategy"].get("liquidity_days", 20),
        min_price=cfg["strategy"].get("min_price"),
    )
    years = (len(md_with.dates)) / 252.0
    for tag, md in (("含", md_with), ("不含", md_wo)):
        cagrs = []
        for seed in range(args.seeds):
            eq = BacktestEngine(md, RandomTopKStrategy(**{**params, "seed": seed})).run()
            cagrs.append((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)
        rows[tag]["random_mean"] = float(np.mean(cagrs))
        rows[tag]["random_std"] = float(np.std(cagrs, ddof=1))

    t = Table(title=f"幸存者偏差对照 ({args.start} ~ , {years:.1f} 年)")
    for c in ["口径", "等权(日)", "等权(5日)", "随机Top%d均值" % args.top_k, "股票数"]:
        t.add_column(c, justify="right")
    for tag in ("不含", "含"):
        r = rows[tag]
        t.add_row(
            f"{tag}退市股",
            f"{r['ew_daily']:+.2%}",
            f"{r['ew_5d']:+.2%}",
            f"{r.get('random_mean', float('nan')):+.2%}",
            str(r["n_sym"]),
        )
    d = rows["含"]
    w = rows["不含"]
    t.add_row(
        "偏差(含-不含)",
        f"{d['ew_daily'] - w['ew_daily']:+.2%}",
        f"{d['ew_5d'] - w['ew_5d']:+.2%}",
        f"{d.get('random_mean', float('nan')) - w.get('random_mean', float('nan')):+.2%}",
        "",
    )
    console.print(t)

    out = pd.DataFrame(rows).T
    out.to_csv("reports/survivorship_bias.csv", encoding="utf-8-sig")
    console.print("[green]saved: reports/survivorship_bias.csv[/green]")


if __name__ == "__main__":
    main()
