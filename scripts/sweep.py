from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import max_drawdown, summarize
from quart.config import load_config
from quart.data.artifacts import ArtifactStore
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.risk.rules import make_weight_validator
from quart.strategy import REGISTRY, build_strategy

console = Console()


def parse_value(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_combo(spec: str) -> dict:
    params = {}
    for token in spec.split(","):
        k, _, v = token.partition("=")
        params[k.strip()] = parse_value(v.strip())
    return params


def yearly_stats(equity: pd.Series) -> dict[str, float]:
    out = {}
    for year, chunk in equity.groupby(equity.index.year):
        if len(chunk) < 2:
            continue
        ret = chunk.iloc[-1] / chunk.iloc[0] - 1.0
        mdd, _ = max_drawdown(chunk)
        out[str(year)] = ret
        out[f"{year}_mdd"] = mdd
    return out


def run_one(
    md: MarketData,
    bench_close: pd.Series,
    strategy_name: str,
    base_params: dict,
    combo: dict,
    initial_cash: float,
    risk_pipeline=None,
):
    params = {**base_params, **combo}
    label_parts = [f"{k}={v}" for k, v in sorted(combo.items())] or ["default"]
    strategy = build_strategy(strategy_name, **params)
    result = BacktestEngine(md, strategy, initial_cash=initial_cash,
                            risk_pipeline=risk_pipeline).run_result()
    equity = result.equity
    trades = result.trades.to_dict("records") if not result.trades.empty else []
    summary = summarize(equity, benchmark=bench_close)
    summary["label"] = " ".join(label_parts)
    summary["n_trades"] = len(trades)
    years = len(equity) / 252.0
    if years > 0 and trades:
        one_side = sum(t["amount"] for t in trades)
        summary["turnover"] = one_side / 2.0 / float(equity.mean()) / years
    else:
        summary["turnover"] = 0.0
    summary.update(yearly_stats(equity))
    return equity, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Parameter sweep with yearly stability breakdown")
    parser.add_argument("--strategy", default=load_config()["strategy"]["name"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--combo", action="append", default=[], help="e.g. --combo \"use_regime_filter=true,regime_filter_days=60\"")
    parser.add_argument("--save-dir", default="reports")
    parser.add_argument("--no-risk", action="store_true",
                        help="关闭回测内风控（默认启用，与实盘同一约束）")
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= args.start) & (args.end is None or bench["date"] <= args.end)]
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    md = MarketData.from_bars(bars, benchmark=bench)
    # 与 run_backtest/pipeline 同口径：板块/ST/次新股过滤（修复后引擎 + 统一过滤的可比基线）
    data_cfg = cfg.get("data", {})
    filtered = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    dropped = bars["symbol"].nunique() - filtered["symbol"].nunique()
    if dropped:
        console.print(f"universe filter: dropped {dropped} symbols, "
                      f"kept {filtered['symbol'].nunique()}")
        md = MarketData.from_bars(filtered, benchmark=bench)
    bench_close = bench.set_index("date")["close"].reindex(md.dates).ffill()
    base_params = {k: v for k, v in cfg["strategy"].items() if k != "name"}
    combos = [parse_combo(c) for c in args.combo] or [{}]

    risk_pipeline = None
    if not args.no_risk:
        risk_pipeline = make_weight_validator(float(cfg["risk"]["max_position_pct"]))

    results = []
    curves = {}
    for combo in combos:
        equity, summary = run_one(
            md, bench_close, args.strategy, base_params, combo,
            float(cfg["backtest"]["initial_cash"]), risk_pipeline=risk_pipeline,
        )
        results.append(summary)
        curves[summary["label"]] = equity
        console.print(f"  done: {summary['label']}  cagr={summary['cagr']:.1%} sharpe={summary['sharpe']:.2f} mdd={summary['max_drawdown']:.1%}")

    metric_cols = ["total_return", "cagr", "sharpe", "max_drawdown", "calmar", "bench_excess_cagr", "turnover", "n_trades"]
    years = sorted({k for r in results for k in r if k.isdigit()})
    table = Table(title=f"Sweep: {args.strategy} ({md.dates[0].date()} ~ {md.dates[-1].date()})")
    table.add_column("params")
    for c in metric_cols:
        table.add_column(c, justify="right")
    for y in years:
        table.add_column(y, justify="right")
    for r in results:
        row = [r["label"]]
        for c in metric_cols:
            v = r.get(c)
            if v is None:
                row.append("-")
            elif c == "n_trades":
                row.append(f"{v}")
            elif c == "turnover":
                row.append(f"{v:.1f}x")
            elif c in ("sharpe", "calmar"):
                row.append(f"{v:.2f}")
            else:
                row.append(f"{v:.1%}")
        for y in years:
            row.append(f"{r.get(y, float('nan')):.1%}")
        table.add_row(*row)
    console.print(table)

    bench_row = ["benchmark(沪深300)"]
    bs = summarize(bench_close)
    for c in metric_cols:
        v = bs.get(c)
        if v is None:
            bench_row.append("-")
        elif isinstance(v, float) and c in ("sharpe", "calmar"):
            bench_row.append(f"{v:.2f}")
        else:
            bench_row.append(f"{v:.1%}")
    bench_row += ["-"] * len(years)
    table.add_row(*bench_row)

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    flat = pd.json_normalize(results)
    flat.to_csv(out_dir / f"sweep_{args.strategy}_{stamp}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(curves).to_csv(out_dir / f"sweep_equity_{args.strategy}_{stamp}.csv")
    console.print(f"[green]saved: sweep_{args.strategy}_{stamp}.csv[/green]")

    # 制品：记录本次扫描的完整参数组合，避免"这张表是哪个网格跑出来的"失考
    run = ArtifactStore().create_run(
        f"sweep_{args.strategy}",
        params={
            "strategy": args.strategy, "combos": combos,
            "base_params": base_params, "start": args.start, "end": args.end,
            "risk_enabled": not args.no_risk,
        },
    )
    run.put_table("results", flat)
    run.put_table("equity_curves", pd.DataFrame(curves))
    best = flat.sort_values("cagr", ascending=False).iloc[0] if not flat.empty else None
    if best is not None:
        run.add_metrics(
            best_label=str(best.get("label")),
            best_cagr=float(best.get("cagr", 0)),
            best_sharpe=float(best.get("sharpe", 0)),
            best_mdd=float(best.get("max_drawdown", 0)),
            n_combos=len(results),
        )
    manifest = run.finish()
    console.print(f"[green]制品目录: artifacts/{manifest.run_id}/[/green]")


if __name__ == "__main__":
    main()
