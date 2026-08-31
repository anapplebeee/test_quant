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
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import PROJECT_ROOT, load_config
from quart.data.quality import load_blocklist
from quart.data.store import BarStore
from quart.research.admission import (
    DEFAULT_THRESHOLDS,
    evaluate_gates,
    write_status,
)

console = Console()


def _run_backtest_cost(strategy: str, cost: float, start: str, end: str | None) -> dict:
    """进程内执行单次回测，返回 summarize() 结果 + n_trades。"""
    from quart.backtest.engine import BacktestEngine
    from quart.backtest.metrics import summarize
    from quart.data.benchmark import equal_weight_benchmark
    from quart.data.market import MarketData
    from quart.data.universe import filter_for_simulation
    from quart.execution.fees import Fees

    cfg = load_config()
    store = BarStore()
    blocked = load_blocklist()
    bars = store.load(start=start, end=end, exclude_symbols=sorted(blocked))
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= start) & (end is None or bench["date"] <= end)]
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )

    from quart.strategy import build_strategy

    strategy_obj = build_strategy(strategy)
    md = MarketData.from_bars(bars, benchmark=bench)
    fees = Fees.from_config().scaled(cost)
    result = BacktestEngine(md, strategy_obj, fees=fees).run_result()

    bench_close = bench.set_index("date")["close"].reindex(result.equity.index).ffill()
    ew_bench = equal_weight_benchmark(result.equity, bars)
    summary = summarize(result.equity, benchmark=bench_close, benchmark2=ew_bench, benchmark2_name="bench2")
    summary["n_trades"] = len(result.trades)
    return summary


def _run_wfa(strategy: str, start: str, end: str | None) -> dict | None:
    """子进程跑 WFA，解析其 artifacts 的 oos_summary。失败返回 None（门禁不通过）。"""
    cmd = [sys.executable, "scripts/walk_forward.py", "--strategy", strategy, "--start", start]
    if end:
        cmd += ["--end", end]
    console.print(f"[blue]running WFA: {' '.join(cmd)}[/blue]")
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[red]WFA failed (exit={proc.returncode})[/red]")
        if proc.stdout:
            console.print(proc.stdout[-800:])
        return None
    # walk_forward 把 oos_summary 写入最新 artifacts run；从 stdout 提取制品目录
    import re

    m = re.search(r"artifacts[/\\](wfa_\S+?)[/\\]\s*\(", proc.stdout)
    if not m:
        console.print("[yellow]WFA 完成但未找到制品目录，尝试按时间取最新 wfa run[/yellow]")
        return None
    manifest = Path(PROJECT_ROOT) / "artifacts" / m.group(1) / "oos_summary.json"
    if not manifest.exists():
        console.print(f"[yellow]未找到 {manifest}[/yellow]")
        return None
    with open(manifest, encoding="utf-8") as f:
        return json.load(f)


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
        cost_summaries[cost] = _run_backtest_cost(args.strategy, cost, args.start, args.end)
        s = cost_summaries[cost]
        console.print(f"  CAGR={s.get('cagr')}, Sharpe={s.get('sharpe')}, MDD={s.get('max_drawdown')}")

    wfa_summary = None if args.skip_wfa else _run_wfa(args.strategy, args.start, args.end)
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
