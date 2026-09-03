"""ht_rotate.py — 3万本金 热门板块轮动+龙头 回测 CLI（本地板块 cluster，规则版）。

用法：
    .venv\\Scripts\\python.exe scripts/ht_rotate.py --start 2025-01-01 --end 2026-09-01
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quart.config import load_config
from quart.data.store import BarStore
from quart.research.ht_backtest import run, summarize
from quart.research.ht_universe import build_pools
from quart.data.universe import get_constituents
import quart.data.universe as univ


def load_sector_map() -> pd.Series:
    from quart.data.universe import _cache_path  # noqa
    df = pd.read_parquet("data/universe/stat_industry.parquet")
    return df.set_index("symbol")["cluster"].astype(str)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load-start", default="2024-01-01", help="数据加载起点(给足 warmup)")
    ap.add_argument("--sim-start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--capital", type=float, default=30_000.0)
    ap.add_argument("--n-leaders", type=int, default=2)
    ap.add_argument("--freq", default="ME")
    ap.add_argument("--hot-rank", type=int, default=1)
    args = ap.parse_args()

    store = BarStore()
    bench_code = load_config()["benchmark"]
    print(f"[rotate] loading bars {args.load_start}..{args.end}")
    bars = store.load(start=args.load_start, end=args.end)
    # 用中证1000作为小盘基准更贴切
    bench_small = store.load_benchmark("000852")

    tr, pos, st = build_pools(bars, capital=args.capital)
    print("[rotate] pool:", st)
    sector = load_sector_map()
    print("[rotate] sectors:", sector.nunique(), "symbols:", len(sector))

    # 用 position_pool(严格3万可负担) 作为可购标的
    res = run(bars, pos, sector, capital=args.capital,
              n_leaders=args.n_leaders, freq=args.freq, hot_rank=args.hot_rank,
              start=args.sim_start)
    summ = summarize(res)
    print("[rotate] result:", json.dumps(summ, ensure_ascii=False))
    # 基准同期（与模拟相同起止）
    b = bench_small[
        (bench_small["date"] >= args.sim_start) & (bench_small["date"] <= args.end)
    ].copy()
    if not b.empty:
        b = b.sort_values("date")
        bt = b["close"].iloc[-1] / b["close"].iloc[0] - 1.0
        print(f"[rotate] CSI1000 same-period total ret: {bt:+.2%}")
    out = "reports/ht_rotate_result.csv"
    res.to_csv(out, index=False)
    print(f"[rotate] saved {out}")
    with open("reports/ht_rotate_meta.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summ, "args": vars(args)}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
