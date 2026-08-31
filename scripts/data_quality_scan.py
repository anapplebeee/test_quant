"""全量数据质量扫描：新鲜度/负价格/OHLC 违规/巨幅跳变分类/停牌/重复日期。

跳变治理（2026-08-31，CODEX_PROGRESS P0）：报告模式只统计；加 --apply 后
把「物理不可能的跳变」（anomaly，见 quart/data/quality.py 分类规则）所在
符号写入阻断清单并隔离其数据文件，回测与更新管线自动排除。

用法：
    uv run python scripts/data_quality_scan.py                 # 只报告
    uv run python scripts/data_quality_scan.py --apply         # 报告 + 阻断
    uv run python scripts/data_quality_scan.py --jumps 0.25
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import PROJECT_ROOT
from quart.data.quality import (
    QUARANTINE_DIR,
    _classify_single,
    build_blocklist,
    load_blocklist,
    quarantine_symbols,
    save_blocklist,
)
from quart.data.store import BarStore

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jumps", type=float, default=0.25)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把 anomaly 符号写入阻断清单并隔离数据文件（默认只报告）",
    )
    args = parser.parse_args()

    store = BarStore()
    symbols = store.symbols()
    console.print(f"scanning {len(symbols)} symbols ...")

    freshness = store.freshness_days()
    console.print(f"data freshness: latest bar is {freshness} day(s) old vs CN-now")

    problems: list[dict] = []
    n_neg = n_ohlc = 0
    n_zero_vol = 0
    dup_total = 0
    ret_stats = []
    jump_rows: list[pd.DataFrame] = []
    per_symbol_jumps = 0

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
        nj = int((ret.abs() > args.jumps).sum())
        per_symbol_jumps += nj
        if nj:
            # 逐符号分类（避免全市场 concat 的内存峰值）
            jr = _classify_single(df.assign(symbol=sym), sym, args.jumps)
            if not jr.empty:
                jump_rows.append(jr)

        zv = int((df["volume"] == 0).sum())
        if zv:
            n_zero_vol += zv

    rs = pd.Series([float(x) for x in ret_stats])

    # ---- 跳变分类（治理核心：合法行情 vs 物理不可能）----
    jump_report = (
        pd.concat(jump_rows, ignore_index=True) if jump_rows else pd.DataFrame()
    )
    class_counts = jump_report["class"].value_counts().to_dict() if not jump_report.empty else {}
    anomaly_syms = build_blocklist(jump_report)
    already_blocked = load_blocklist()
    new_blocked = [s for s in anomaly_syms if s not in already_blocked]

    table = Table(title="数据质量扫描")
    table.add_column("检查项", justify="left")
    table.add_column("结果", justify="right")
    table.add_row("股票数", str(len(symbols)))
    table.add_row("新鲜度（最新bar距今）", f"{freshness} 天")
    table.add_row("负/零价格行", str(n_neg))
    table.add_row("OHLC 违规行", str(n_ohlc))
    table.add_row(f"单日|ret|>{args.jumps:.0%} 跳变行", str(per_symbol_jumps))
    for cls in ("resume_gap", "new_stock", "limit_move", "anomaly"):
        table.add_row(f"  ↳ {cls}", str(class_counts.get(cls, 0)))
    table.add_row("volume==0 行（停牌）", str(n_zero_vol))
    table.add_row("重复日期行", str(dup_total))
    table.add_row("个股|日收益|中位数(全市场均值)", f"{rs.mean():.4f}" if len(rs) else "-")
    table.add_row("anomaly 符号数（物理不可能跳变）", str(len(anomaly_syms)))
    table.add_row("已阻断 / 本次新增", f"{len(already_blocked)} / {len(new_blocked)}")
    console.print(table)

    out = PROJECT_ROOT / "reports" / "data_quality_scan.csv"
    problems_df = pd.DataFrame(problems)
    if not jump_report.empty:
        problems_df = pd.concat([problems_df, jump_report.assign(issue="jump_" + jump_report["class"]).drop(columns=["class"])], ignore_index=True)
    problems_df.to_csv(out, index=False)
    console.print(f"saved: {out}")

    if args.apply:
        save_blocklist(sorted(set(anomaly_syms) | already_blocked))
        moved = quarantine_symbols(store, new_blocked)
        console.print(
            f"[green]--apply 完成[/green]: 阻断清单 {len(set(anomaly_syms) | already_blocked)} 个符号，"
            f"隔离 {len(moved)} 个数据文件 -> {QUARANTINE_DIR}"
        )
    elif anomaly_syms:
        console.print(
            f"[yellow]发现 {len(anomaly_syms)} 个 anomaly 符号，"
            f"加 --apply 执行阻断[/yellow]"
        )


if __name__ == "__main__":
    main()
