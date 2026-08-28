from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel

from quart.backtest.engine import BacktestEngine, MarketData
from quart.backtest.metrics import format_summary, summarize
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy import build_strategy

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run A-share backtest")
    parser.add_argument("--strategy", default=load_config()["strategy"]["name"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-regime", action="store_true")
    parser.add_argument("--save-dir", default="reports")
    args = parser.parse_args()

    cfg = load_config()
    store = BarStore()
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= args.start) & (args.end is None or bench["date"] <= args.end)]
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
    )
    if bars.empty:
        raise SystemExit("过滤板块/ST 后无可用标的，请检查 data 配置或本地数据")

    params = dict(cfg["strategy"])
    params.pop("name", None)
    if args.no_regime:
        params["use_regime_filter"] = False
    strategy = build_strategy(args.strategy, **params)

    md = MarketData.from_bars(bars, benchmark=bench)
    engine = BacktestEngine(md, strategy)
    equity = engine.run()
    trades_df = pd.DataFrame([t.__dict__ for t in engine.trades])

    bench_close = bench.set_index("date")["close"].reindex(equity.index).ffill()
    summary = summarize(equity, benchmark=bench_close)

    console.print(Panel(f"策略: {args.strategy}  |  交易笔数: {len(trades_df)}", title="Quart Backtest"))
    console.print(format_summary(summary))

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    equity.to_frame("equity").to_csv(out_dir / f"equity_{args.strategy}_{stamp}.csv")
    if not trades_df.empty:
        trades_df.to_csv(out_dir / f"trades_{args.strategy}_{stamp}.csv", index=False)
    with open(out_dir / f"summary_{args.strategy}_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    console.print(f"[green]结果已保存到 {out_dir}/[/green]")


if __name__ == "__main__":
    main()
