"""全量数据质量扫描：覆盖率/新鲜度/负价格/OHLC 违规/巨幅跳变分类/停牌/重复日期/成交量单位。

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

from quart.config import reports_dir
from quart.data.artifacts import ArtifactStore
from quart.data.quality import (
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
    coverage = store.coverage_summary()
    console.print(f"data freshness: latest bar is {freshness} day(s) old vs CN-now")

    problems: list[dict] = []
    n_neg = n_ohlc = 0
    n_zero_vol = 0
    n_missing_amount = 0
    n_volume_unit = 0
    n_stale_symbols = 0
    dup_total = 0
    ret_stats = []
    jump_rows: list[pd.DataFrame] = []
    per_symbol_jumps = 0

    for sym in symbols:
        df = store.load(symbols=[sym])
        if df.empty:
            continue
        df = df.sort_values("date")
        latest_date = pd.Timestamp(df["date"].max())
        market_latest = pd.Timestamp(coverage["latest_date"])
        lag_days = int((market_latest - latest_date).days)
        if lag_days > 30:
            n_stale_symbols += 1
            problems.append({
                "symbol": sym,
                "issue": "stale_symbol_gt_30d",
                "count": 1,
                "latest_date": str(latest_date.date()),
                "lag_days": lag_days,
            })
        dup = int(df["date"].duplicated().sum())
        dup_total += dup
        if dup:
            problems.append({"symbol": sym, "issue": "dup_dates", "count": dup})

        closes = df["close"]
        neg = int((closes <= 0).sum())
        if neg:
            n_neg += neg
            problems.append({"symbol": sym, "issue": "non_positive_close", "count": neg})

        opens, highs, lows, closes = df["open"], df["high"], df["low"], df["close"]
        valid = closes > 0
        ohlc_bad = int((
            (highs < lows)
            | (opens > highs + 1e-6)
            | (opens < lows - 1e-6)
            | (closes > highs + 1e-6)
            | (closes < lows - 1e-6)
        )[valid].sum())
        if ohlc_bad:
            n_ohlc += ohlc_bad
            problems.append({"symbol": sym, "issue": "ohlc_violation", "count": ohlc_bad})

        ret = closes.pct_change(fill_method=None)
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

        positive_volume = df["volume"] > 0
        missing_amount = int((positive_volume & (df["amount"].isna() | (df["amount"] <= 0))).sum())
        if missing_amount:
            n_missing_amount += missing_amount
            problems.append({"symbol": sym, "issue": "missing_amount", "count": missing_amount})

        unit_ratio = (
            df.loc[positive_volume, "amount"]
            / (df.loc[positive_volume, "close"] * df.loc[positive_volume, "volume"])
        ).replace([np.inf, -np.inf], np.nan).median()
        if pd.notna(unit_ratio) and not 50 <= float(unit_ratio) <= 150:
            n_volume_unit += 1
            problems.append({
                "symbol": sym,
                "issue": "volume_unit_not_hand",
                "count": 1,
                "amount_close_volume_ratio": float(unit_ratio),
            })

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
    table.add_row(
        "最新日横截面覆盖",
        f"{coverage['latest_symbols']}/{coverage['symbols']} ({coverage['latest_coverage']:.1%})",
    )
    table.add_row("新鲜度（最新bar距今）", f"{freshness} 天")
    table.add_row("负/零价格行", str(n_neg))
    table.add_row("OHLC 违规行", str(n_ohlc))
    table.add_row(f"单日|ret|>{args.jumps:.0%} 跳变行", str(per_symbol_jumps))
    for cls in ("resume_gap", "new_stock", "limit_move", "anomaly"):
        table.add_row(f"  ↳ {cls}", str(class_counts.get(cls, 0)))
    table.add_row("volume==0 行（停牌）", str(n_zero_vol))
    table.add_row("有成交但成交额缺失", str(n_missing_amount))
    table.add_row("成交量单位异常股票", str(n_volume_unit))
    table.add_row("落后市场最新日>30天股票", str(n_stale_symbols))
    table.add_row("重复日期行", str(dup_total))
    table.add_row("个股|日收益|中位数(全市场均值)", f"{rs.mean():.4f}" if len(rs) else "-")
    table.add_row("anomaly 符号数（物理不可能跳变）", str(len(anomaly_syms)))
    table.add_row("已阻断 / 本次新增", f"{len(already_blocked)} / {len(new_blocked)}")
    console.print(table)

    # 完整问题明细落 reports/（含跳变分类）
    out = reports_dir() / "data_quality_scan.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    problems_df = pd.DataFrame(problems)
    if not jump_report.empty:
        problems_df = pd.concat(
            [problems_df,
             jump_report.assign(issue="jump_" + jump_report["class"]).drop(columns=["class"])],
            ignore_index=True,
        )
    problems_df.to_csv(out, index=False)

    # 汇总与明细同时入制品仓库（run_id 可追溯）
    summary = {
        "symbols": len(symbols),
        "freshness_days": freshness,
        **coverage,
        "non_positive_close": n_neg,
        "ohlc_violations": n_ohlc,
        "large_jumps": per_symbol_jumps,
        "anomaly_symbols": len(anomaly_syms),
        "blocked_total": len(set(anomaly_syms) | already_blocked),
        "zero_volume_rows": n_zero_vol,
        "missing_amount_rows": n_missing_amount,
        "volume_unit_bad_symbols": n_volume_unit,
        "stale_symbols_gt_30d": n_stale_symbols,
        "duplicate_rows": dup_total,
    }
    writer = ArtifactStore().create_run("data_quality", {"jump_threshold": args.jumps})
    writer.put_table("problems", problems_df)
    writer.put_json("summary", summary)
    writer.add_metrics(**summary)
    manifest = writer.finish()

    console.print(f"saved: {out}")
    console.print(f"artifact: {manifest.run_id}")

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
