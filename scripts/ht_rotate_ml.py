"""ht_rotate_ml.py — 3万本金 热门板块轮动 + ML分数选龙头 回测。

与 ht_rotate.py 相同框架，但在热门板块内用 LightGBM 预测分数(ht_ml_scores.csv)
替代"板块内动量"选龙头。对照规则动量版看能否降回撤/提收益。
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quart.config import load_config
from quart.data.store import BarStore
from quart.research.ht_backtest import run, summarize
from quart.research.ht_universe import build_pools


def load_sector_map() -> pd.Series:
    df = pd.read_parquet("data/universe/stat_industry.parquet")
    return df.set_index("symbol")["cluster"].astype(str)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load-start", default="2024-01-01")
    ap.add_argument("--sim-start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--capital", type=float, default=30_000.0)
    ap.add_argument("--n-leaders", type=int, default=2)
    ap.add_argument("--freq", default="ME")
    ap.add_argument("--hot-rank", type=int, default=1)
    ap.add_argument("--scores", default="reports/ht_ml_scores.csv")
    args = ap.parse_args()

    store = BarStore()
    bench_code = load_config()["benchmark"]
    bars = store.load(start=args.load_start, end=args.end)
    bench_small = store.load_benchmark("000852")
    tr, pos, st = build_pools(bars, capital=args.capital)
    print("[rotate_ml] pool:", st)
    sector = load_sector_map()
    print("[rotate_ml] sectors:", sector.nunique())

    score = pd.read_csv(args.scores)
    print("[rotate_ml] score:", score.shape)
    score["date"] = pd.to_datetime(score["date"])

    res = run(bars, pos, sector, score=score, capital=args.capital,
              n_leaders=args.n_leaders, freq=args.freq, hot_rank=args.hot_rank,
              start=args.sim_start)
    summ = summarize(res)
    print("[rotate_ml] result:", json.dumps(summ, ensure_ascii=False))
    b = bench_small[(bench_small["date"] >= args.sim_start) & (bench_small["date"] <= args.end)]
    if not b.empty:
        b = b.sort_values("date")
        print(f"[rotate_ml] CSI1000 same-period total ret: {b['close'].iloc[-1]/b['close'].iloc[0]-1:+.2%}")
    res.to_csv("reports/ht_rotate_ml_result.csv", index=False)
    with open("reports/ht_rotate_ml_meta.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summ, "args": vars(args)}, f, ensure_ascii=False, indent=2)
    print("[rotate_ml] saved reports/ht_rotate_ml_result.csv")


if __name__ == "__main__":
    main()
