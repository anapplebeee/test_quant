"""Evaluate close-confirmed A-share consecutive limit-up events.

Examples:
    uv run python scripts/eval_limit_streak.py --start 2023-01-01 --end 2026-08-31
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from pathlib import Path

from rich.console import Console
from rich.table import Table

from quart.config import data_root, load_config
from quart.data.artifacts import STATUS_FAILED, ArtifactStore
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.limit_streak import (
    build_limit_streak_events,
    summarize_limit_streak_events,
    summarize_limit_streak_progression,
)

console = Console()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_ints(raw: str) -> tuple[int, ...]:
    return tuple(int(value.strip()) for value in raw.split(",") if value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股连板事件与可成交性分析")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--levels", default="1,2,3,4,5", help="首次达到的连板级别")
    parser.add_argument("--horizons", default="1,2,3,5", help="T+1 开盘入场后的持有日数")
    parser.add_argument("--split-date", default="2025-01-01")
    parser.add_argument("--min-avg-amount", type=float, default=50_000_000.0)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--max-exit-delay", type=int, default=5)
    parser.add_argument("--benchmark", default="000852", help="市场基准，默认中证1000")
    args = parser.parse_args()

    levels = _parse_ints(args.levels)
    horizons = _parse_ints(args.horizons)
    cfg = load_config()
    backtest_cfg = cfg["backtest"]
    cost_bps = 10_000 * (
        2 * float(backtest_cfg["commission_rate"])
        + float(backtest_cfg["stamp_tax_rate"])
        + 2 * float(backtest_cfg["transfer_fee_rate"])
        + 2 * float(backtest_cfg["slippage_rate"])
    )
    params = {
        **vars(args),
        "levels": levels,
        "horizons": horizons,
        "cost_bps_1x": cost_bps,
        "signal_timing": "T close",
        "entry_timing": "T+1 open",
    }
    writer = ArtifactStore().create_run("limit_streak_research", params)
    try:
        store = BarStore()
        bars = store.load(start=args.start, end=args.end, include_index=False)
        data_cfg = cfg.get("data", {})
        bars = filter_for_simulation(
            bars,
            exclude_star=bool(data_cfg.get("exclude_star", True)),
            exclude_chinext=bool(data_cfg.get("exclude_chinext", True)),
            exclude_st=bool(data_cfg.get("exclude_st", True)),
            min_list_days=int(data_cfg.get("min_list_days", 0)),
        )
        if bars.empty:
            raise RuntimeError("本地行情为空")
        benchmark = store.load_benchmark(args.benchmark)
        market = MarketData.from_bars(bars, benchmark)
        benchmark_open = (
            benchmark.sort_values("date").set_index("date")["open"]
            if not benchmark.empty and "open" in benchmark
            else None
        )
        events = build_limit_streak_events(
            market,
            benchmark_open=benchmark_open,
            levels=levels,
            horizons=horizons,
            min_avg_amount=args.min_avg_amount,
            max_exit_delay=args.max_exit_delay,
        )
        summary, periods = summarize_limit_streak_events(
            events,
            cost_bps=cost_bps,
            split_date=args.split_date,
            max_positions=args.max_positions,
        )
        progression = summarize_limit_streak_progression(
            events, max_positions=args.max_positions
        )
        if summary.empty:
            raise RuntimeError("没有满足条件的连板事件")

        root = Path(data_root())
        evidence = {
            "security_master": (root / "meta" / "security_master.parquet").exists(),
            "delisted_history": (root / "meta" / "delisted.parquet").exists(),
            "corporate_actions": (root / "meta" / "corporate_actions.parquet").exists(),
            "price_adjustment": cfg.get("data", {}).get("adjust"),
            "benchmark": args.benchmark,
            "source_hashes": {
                "quart/research/limit_streak.py": _sha256(
                    Path("quart/research/limit_streak.py")
                ),
                "scripts/eval_limit_streak.py": _sha256(
                    Path("scripts/eval_limit_streak.py")
                ),
            },
            "formal_status": "PROVISIONAL",
            "reason": (
                "当前涨停识别使用日线复权价与探索层 RuleBook；事件研究尚未经过正式组合"
                "回测/WFA，缺失任一 PIT 证据时不得晋级 Paper"
            ),
        }
        decision = "PROVISIONAL_CANDIDATE" if bool(summary["candidate_gate"].any()) else "FAIL"
        writer.put_table("events", events)
        writer.put_table("summary", summary)
        writer.put_table("periods", periods)
        writer.put_table("progression", progression)
        writer.put_json("evidence", evidence)
        writer.add_metrics(
            decision=decision,
            events=int(events[["signal_date", "symbol", "streak_level"]].drop_duplicates().shape[0]),
            candidate_variants=int(summary["candidate_gate"].sum()),
            cost_bps_1x=cost_bps,
        )
        manifest = writer.finish()

        report_dir = Path("reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_path = report_dir / f"limit_streak_summary_{stamp}.csv"
        periods_path = report_dir / f"limit_streak_periods_{stamp}.csv"
        progression_path = report_dir / f"limit_streak_progression_{stamp}.csv"
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        periods.to_csv(periods_path, index=False, encoding="utf-8-sig")
        progression.to_csv(progression_path, index=False, encoding="utf-8-sig")

        table = Table(title=f"连板事件研究（1x 成本 {cost_bps:.1f}bp，状态 {decision}）")
        for column in ("板", "持有", "事件", "买拒", "毛收益", "1x净", "超额EW", "前段", "后段", "FDR", "门禁"):
            table.add_column(column, justify="right")
        for _, row in summary.iterrows():
            table.add_row(
                str(int(row["streak_level"])),
                str(int(row["horizon"])),
                str(int(row["signals"])),
                f"{row['entry_block_rate']:.1%}",
                f"{row['daily_basket_return']:+.2%}",
                f"{row['net_1x']:+.2%}",
                f"{row['excess_eligible']:+.2%}",
                f"{row['early_excess_eligible']:+.2%}",
                f"{row['late_excess_eligible']:+.2%}",
                f"{row['fdr_qvalue']:.3f}",
                "PASS" if bool(row["candidate_gate"]) else "FAIL",
            )
        console.print(table)
        progression_table = Table(title="连板次日晋级与开盘排队风险")
        for column in (
            "板", "事件", "晋级率", "一字率", "Top10晋级", "Top10买拒", "可买晋级", "晋级捕获"
        ):
            progression_table.add_column(column, justify="right")
        for _, row in progression.iterrows():
            progression_table.add_row(
                str(int(row["streak_level"])),
                str(int(row["events"])),
                f"{row['promotion_rate']:.1%}",
                f"{row['one_word_rate']:.1%}",
                f"{row['selected_promotion_rate']:.1%}",
                f"{row['selected_open_limit_block_rate']:.1%}",
                f"{row['selected_buyable_promotion_rate']:.1%}",
                f"{row['promotion_capture_rate']:.1%}",
            )
        console.print(progression_table)
        console.print(f"artifact: {manifest.run_id}")
        console.print(f"saved: {summary_path} / {periods_path} / {progression_path}")
        console.print(
            "[yellow]该结果是探索性事件研究；只有通过正式组合回测、连续 WFA、"
            "成本容量与完整 PIT 证据后，才可申请 Paper。[/yellow]"
        )
    except Exception as exc:
        writer.finish(status=STATUS_FAILED, error=str(exc))
        raise


if __name__ == "__main__":
    main()
