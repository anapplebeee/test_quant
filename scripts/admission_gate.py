"""策略准入自动化门禁 CLI。

对指定策略执行：0/1/2 倍成本压力回测（进程内）→ WFA 样本外验证（子进程，
可用 --skip-wfa 显式跳过但门禁判不通过）→ 阈值评估 → 写入准入台账。

用法：
    uv run python scripts/admission_gate.py --strategy momentum_rotation
    uv run python scripts/admission_gate.py --strategy lowvol_indz --start 2022-01-01
    uv run python scripts/admission_gate.py --strategy X --skip-wfa   # 仅诊断，不会 PASS
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import load_config
from quart.research.admission import (
    DEFAULT_THRESHOLDS,
    evaluate_gates,
    write_status,
)
from quart.research.formal_audit import run_cost_stress, run_wfa_subprocess

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--skip-wfa", action="store_true",
        help="跳过 WFA（门禁将判 FAIL，仅用于诊断）",
    )
    parser.add_argument(
        "--no-apply", action="store_true",
        help="只打印结果，不写准入台账",
    )
    args = parser.parse_args()

    cfg = load_config()
    thresholds = {**DEFAULT_THRESHOLDS, **{k: float(v) for k, v in cfg.get("admission", {}).items()}}

    cost_summaries: dict[float, dict] = {}
    for cost in (0.0, 1.0, 2.0):
        console.print(f"[blue]backtest {args.strategy} @ {cost:g}x cost ...[/blue]")
        cost_summaries.update(
            run_cost_stress(args.strategy, args.start, args.end, multipliers=(cost,))
        )
        s = cost_summaries[cost]
        console.print(f"  CAGR={s.get('cagr')}, Sharpe={s.get('sharpe')}, MDD={s.get('max_drawdown')}")

    wfa_summary = None if args.skip_wfa else run_wfa_subprocess(args.strategy, args.start, args.end)
    result = evaluate_gates(cost_summaries, wfa_summary, thresholds)

    table = Table(title=f"准入门禁: {args.strategy}")
    table.add_column("检查项")
    table.add_column("结果")
    table.add_column("详情")
    for c in result.checks:
        color = "green" if c["status"] == "PASS" else "red"
        table.add_row(c["check"], f"[{color}]{c['status']}[/{color}]", c["detail"])
    console.print(table)
    verdict = "[green]PASS —— 可申请加入 live_allowlist[/green]" if result.passed else "[red]FAIL —— 禁止晋级实盘[/red]"
    console.print(f"门禁结论: {verdict}")

    if not args.no_apply:
        write_status(args.strategy, result, thresholds)
        console.print("准入台账已更新: data/meta/admission_status.csv")

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
