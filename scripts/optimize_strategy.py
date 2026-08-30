"""策略优化实验：复用一次数据加载，批量跑参数网格并输出对比表。

背景（2026-08-31，GLM 策略诊断）：
本地 BarStore 仅 229 只标的（全市场口径应为 3215 只），本实验在当前
本地池上量化各参数与择时开关对成本后收益的贡献，并给出优化建议。
结论写入 reports/strategy_optimization_2026-08-31.md。

用法:
    .venv/Scripts/python.exe scripts/optimize_strategy.py \
        --strategy lowvol_indz --start 2020-01-01
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import summarize
from quart.config import load_config
from quart.data.benchmark import equal_weight_benchmark
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.execution.fees import Fees
from quart.risk.rules import make_weight_validator
from quart.strategy import build_strategy

console = Console()


def run_one(md: MarketData, strategy_name: str, bars: pd.DataFrame, bench: pd.DataFrame,
            params: dict, fees: Fees | None, risk_cfg: dict) -> dict:
    strategy = build_strategy(strategy_name, **params)
    violations: list[str] = []
    risk_pipeline = make_weight_validator(float(risk_cfg["max_position_pct"]), collect=violations)
    result = BacktestEngine(md, strategy, risk_pipeline=risk_pipeline, fees=fees).run_result()
    equity = result.equity
    bench_close = bench.set_index("date")["close"].reindex(equity.index).ffill()
    ew = equal_weight_benchmark(equity, bars)
    s = summarize(equity, benchmark=bench_close, benchmark2=ew, benchmark2_name="ew")
    s["n_trades"] = len(result.trades)
    return s


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="lowvol_indz")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--save", default="reports/strategy_optimization_2026-08-31.md")
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= args.start) & (args.end is None or bench["date"] <= args.end)]
    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    n_symbols = bars["symbol"].nunique()
    console.print(f"[cyan]股票池: {n_symbols} 只, 区间 {args.start} ~ {args.end or 'latest'}[/cyan]")

    md = MarketData.from_bars(bars, benchmark=bench)
    risk_cfg = cfg["risk"]
    zero_fees = Fees(commission_rate=0.0, commission_min=0.0, stamp_tax_rate=0.0,
                     transfer_fee_rate=0.0, slippage_rate=0.0, impact_coef=0.0)

    # 实验矩阵：label -> (params, zero_cost)
    experiments: list[tuple[str, dict, Fees | None]] = [
        ("A0 基线(45d/top30/buf.5/择时)", {}, None),
        ("A1 关闭择时", {"use_regime_filter": False}, None),
        ("A2 top20", {"top_k": 20}, None),
        ("A3 top50", {"top_k": 50}, None),
        ("A4 rebalance20(高换手)", {"rebalance_days": 20}, None),
        ("A5 rebalance60(低频)", {"rebalance_days": 60}, None),
        ("A6 无排名缓冲(buffer0)", {"rank_buffer": 0.0}, None),
        ("A7 反转叠加 rev0.3", {"rev_weight": 0.3}, None),
        ("A8 宽迟滞带 band0.05", {"regime_band": 0.05}, None),
        ("A9 零成本(A0 同参)", {}, zero_fees),
    ]

    rows: list[dict] = []
    for label, params, fees in experiments:
        t0 = time.perf_counter()
        try:
            s = run_one(md, args.strategy, bars, bench, params, fees, risk_cfg)
            s["label"] = label
            s["seconds"] = round(time.perf_counter() - t0, 1)
            rows.append(s)
            console.print(
                f"[green]✓ {label}: CAGR {s['cagr']:+.1%} Sharpe {s['sharpe']:.2f} "
                f"MDD {s['max_drawdown']:.1%} trades {s['n_trades']} ({s['seconds']}s)[/green]"
            )
        except Exception as exc:
            console.print(f"[red]✗ {label}: {exc}[/red]")
            rows.append({"label": label, "error": str(exc)})

    table = Table(title=f"{args.strategy} 参数实验 ({args.start}~, 池 {n_symbols} 只)")
    for col in ("实验", "CAGR", "Sharpe", "MDD", "Calmar", "总收益", "笔数", "对等权超额"):
        table.add_column(col)
    for s in rows:
        if "error" in s:
            table.add_row(s["label"], "ERROR", "", "", "", "", "", "")
            continue
        table.add_row(
            s["label"],
            f"{s['cagr']:+.2%}", f"{s['sharpe']:.2f}", f"{s['max_drawdown']:.1%}",
            f"{s['calmar']:.2f}", f"{s['total_return']:+.1%}", str(s["n_trades"]),
            f"{s.get('bench2_excess_cagr', float('nan')):+.1%}",
        )
    console.print(table)

    out = Path(args.save)
    out.parent.mkdir(exist_ok=True)
    payload = {
        "strategy": args.strategy,
        "start": args.start,
        "end": args.end,
        "n_symbols": int(n_symbols),
        "rows": [{k: v for k, v in s.items() if not isinstance(v, pd.Series)} for s in rows],
    }
    out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]实验数据已保存: {out.with_suffix('.json')}[/green]")


if __name__ == "__main__":
    main()
