"""Comprehensive factor audit with rolling IC, redundancy and T+1 labels."""

from __future__ import annotations

import argparse
import json

from rich.console import Console
from rich.table import Table

from common import reports_dir
from quart.config import load_config
from quart.data.artifacts import STATUS_FAILED, ArtifactStore
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.factor_audit import run_factor_audit

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a comprehensive A-share factor audit")
    parser.add_argument("--sample", choices=["monthly", "weekly"], default="monthly")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--min-amount", type=float, default=20_000_000)
    parser.add_argument("--min-cross-section", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=260, help="评估前预热交易日数")
    parser.add_argument("--evaluation-start", default=None, help="只统计该日及之后的样本（仍保留前置预热）")
    parser.add_argument("--evaluation-end", default=None, help="只统计该日及之前的样本")
    parser.add_argument("--factor", action="append", default=[], help="只审计指定因子，可重复")
    args = parser.parse_args()

    params = vars(args)
    writer = ArtifactStore().create_run("factor_audit", params)
    try:
        config = load_config()
        store = BarStore()
        bars = store.load(start=args.start, end=args.end, include_index=False)
        bars = filter_for_simulation(
            bars,
            exclude_star=bool(config["data"].get("exclude_star", True)),
            exclude_chinext=bool(config["data"].get("exclude_chinext", True)),
            exclude_st=bool(config["data"].get("exclude_st", True)),
            min_list_days=int(config["data"].get("min_list_days", 0)),
        )
        benchmark = store.load_benchmark(config["benchmark"])
        market = MarketData.from_bars(bars, benchmark)
        result = run_factor_audit(
            market,
            sample=args.sample,
            horizon=args.horizon,
            min_amount=args.min_amount,
            min_cross_section=args.min_cross_section,
            warmup=args.warmup,
            factor_names=args.factor or None,
            evaluation_start=args.evaluation_start,
            evaluation_end=args.evaluation_end,
        )

        writer.put_table("summary", result.summary)
        writer.put_table("ic_history", result.ic_history)
        writer.put_table("correlation", result.correlation.reset_index(names="factor"))
        writer.put_table("provisional_baseline", result.baseline)
        writer.put_json("metadata", result.metadata)
        writer.add_metrics(
            factors=result.metadata["factor_count"],
            symbols=result.metadata["symbols"],
            sample_points=result.metadata["sample_points"],
        )
        manifest = writer.finish()

        output_dir = reports_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        result.summary.to_csv(output_dir / "factor_audit_summary.csv", index=False)
        result.ic_history.to_csv(output_dir / "factor_audit_ic_history.csv", index=False)
        result.correlation.to_csv(output_dir / "factor_audit_correlation.csv")
        result.baseline.to_csv(output_dir / "factor_audit_provisional_baseline.csv", index=False)
        with open(output_dir / "factor_audit_metadata.json", "w", encoding="utf-8") as file:
            json.dump(result.metadata, file, ensure_ascii=False, indent=2)

        table = Table(title="A 股因子审计（临时基线；全部因子均按越高越优定向）")
        for column in ["因子", "状态", "IC", "ICIR", "后半段", "近期", "覆盖", "相关"]:
            table.add_column(column, justify="right")
        for _, row in result.summary.iterrows():
            table.add_row(
                str(row["factor"]),
                str(row["status"]),
                f"{row['ic']:+.4f}",
                f"{row['icir']:+.2f}",
                f"{row['late_ic']:+.4f}",
                f"{row['recent_ic']:+.4f}",
                f"{row['coverage']:.0%}",
                f"{row['max_abs_corr']:.2f}",
            )
        console.print(table)
        console.print(
            "[yellow]PROVISIONAL[/yellow] 当前结果仅供研究：行情/指数快照已记录，但逐日 PIT 股票池、"
            "历史证券状态和财报实际披露时间仍不完整；不得改变正式准入或 live allowlist。"
        )
        console.print(f"[green]artifact[/green] {manifest.run_id}")
    except Exception as exc:
        writer.finish(status=STATUS_FAILED, error=str(exc))
        raise


if __name__ == "__main__":
    main()
