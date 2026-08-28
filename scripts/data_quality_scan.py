"""全量数据质量扫描（增量版）：新鲜度/负价格/OHLC 违规/巨幅跳变/停牌/重复日期。

用法：uv run python scripts/data_quality_scan.py [--jumps 0.25]
输出：控制台摘要 + reports/data_quality_scan.csv（问题股票清单）
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

from quart.config import PROJECT_ROOT
from quart.data.store import BarStore

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jumps", type=float, default=0.25)
    args = parser.parse_args()

    store = BarStore()
    symbols = store.symbols()
    console.print(f"scanning {len(symbols)} symbols ...")

    freshness = store.freshness_days()
    console.print(f"data freshness: latest bar is {freshness} day(s) old vs CN-now")

    problems: list[dict] = []
    n_neg = n_ohlc = n_jump = 0
    n_zero_vol = 0
    dup_total = 0
    ret_stats = []

    for sym in symbols:
        df = pd.read_parquet(store._path(sym))  # noqa: SLF001 - 扫描脚本内部使用
        if df.empty:
            continue
        df = df.sort_values("date")
        dup = int(df["date"].duplicated().sum())
        dup_total += dup
        if dup:
            problems.append({"symbol": sym, "issue": "dup_dates", "count": dup})

        closes = df["close"]
        neg = int((closes <= 0).sum())
        if neg:
            n_neg += neg
            problems.append({"symbol": sym, "issue": "non_positive_close", "count": neg})

        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        valid = c > 0
        ohlc_bad = int(((h < l) | (o > h + 1e-6) | (o < l - 1e-6) | (c > h + 1e-6) | (c < l - 1e-6))[valid].sum())
        if ohlc_bad:
            n_ohlc += ohlc_bad
            problems.append({"symbol": sym, "issue": "ohlc_violation", "count": ohlc_bad})

        ret = c.pct_change(fill_method=None)
        ret_stats.append(ret.abs().median())
        jumps = ret.abs() > args.jumps
        # 排除停牌复牌首日（volume==0 前一日）不算跳变证据不足，仅统计真实跳变
        nj = int(jumps.sum())
        if nj:
            n_jump += nj
            worst = ret.abs().idxmax()
            problems.append({
                "symbol": sym, "issue": f"jump_gt_{args.jumps}",
                "count": nj,
                "worst_date": str(pd.Timestamp(df.loc[worst, "date"]).date()),
                "worst_ret": float(ret.loc[worst]),
            })

        zv = int((df["volume"] == 0).sum())
        if zv:
            n_zero_vol += zv

    rs = pd.Series([float(x) for x in ret_stats])
    table = Table(title="数据质量扫描")
    table.add_column("检查项", justify="left")
    table.add_column("结果", justify="right")
    table.add_row("股票数", str(len(symbols)))
    table.add_row("新鲜度（最新bar距今）", f"{freshness} 天")
    table.add_row("负/零价格行", str(n_neg))
    table.add_row("OHLC 违规行", str(n_ohlc))
    table.add_row(f"单日|ret|>{args.jumps:.0%} 跳变", str(n_jump))
    table.add_row("volume==0 行（停牌）", str(n_zero_vol))
    table.add_row("重复日期行", str(dup_total))
    table.add_row("个股|日收益|中位数(全市场均值)", f"{rs.mean():.4f}" if len(rs) else "-")
    console.print(table)

    out = PROJECT_ROOT / "reports" / "data_quality_scan.csv"
    pd.DataFrame(problems).to_csv(out, index=False)
    console.print(f"saved: {out}")


if __name__ == "__main__":
    main()
