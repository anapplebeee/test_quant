from __future__ import annotations

import common

import argparse
import datetime as dt
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import format_summary, summarize
from quart.config import load_config
from quart.data.artifacts import ArtifactStore
from quart.data.benchmark import equal_weight_benchmark
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.risk.rules import make_weight_validator
from quart.strategy import build_strategy

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run A-share backtest")
    parser.add_argument("--strategy", default=load_config()["strategy"]["name"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-regime", action="store_true")
    parser.add_argument("--save-dir", default=str(common.reports_dir()))
    # 前端可调参数：显式传入时覆盖 config（含 config.strategy.overrides 按策略覆盖）
    parser.add_argument("--rebalance-days", type=int, default=None,
                        help="换手频率（交易日），覆盖 config")
    parser.add_argument("--top-k", type=int, default=None, help="持仓数量，覆盖 config")
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

    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    if bars.empty:
        raise SystemExit("过滤板块/ST 后无可用标的，请检查 data 配置或本地数据")

    explicit_params = {}
    if args.no_regime:
        explicit_params["use_regime_filter"] = False
    if args.rebalance_days is not None:
        explicit_params["rebalance_days"] = args.rebalance_days
    if args.top_k is not None:
        explicit_params["top_k"] = args.top_k
    strategy = build_strategy(args.strategy, **explicit_params)
    effective_params = dict(strategy.params)

    md = MarketData.from_bars(bars, benchmark=bench)
    # 风控进回测：默认与实盘同一约束，否则回测组合可以违反单票上限而实盘被截断
    violations: list[str] = []
    risk_pipeline = None
    if not args.no_risk:
        risk_pipeline = make_weight_validator(
            float(cfg["risk"]["max_position_pct"]), collect=violations
        )

    # 产出同时写 artifacts/（可追溯：run_id + 参数 + 数据版本 + 指纹）
    # 与 reports/（兼容现有 api/frontend）
    run = ArtifactStore().create_run(
        f"backtest_{args.strategy}",
        params={
            "strategy": args.strategy,
            **effective_params,
            "start": args.start, "end": args.end,
            "no_regime": args.no_regime, "risk_enabled": not args.no_risk,
        },
    )

    try:
        result = BacktestEngine(md, strategy, risk_pipeline=risk_pipeline).run_result()
    except Exception as exc:
        run.finish(status="failed", error=str(exc))
        raise

    equity = result.equity
    trades_df = result.trades

    bench_close = bench.set_index("date")["close"].reindex(equity.index).ffill()
    # 等权基准：与策略同股票池（已过滤板块/ST）的每日等权组合，衡量选股 alpha
    ew_bench = equal_weight_benchmark(equity, bars)
    summary = summarize(equity, benchmark=bench_close, benchmark2=ew_bench, benchmark2_name="bench2")

    console.print(Panel(f"策略: {args.strategy}  |  交易笔数: {len(trades_df)}", title="Quart Backtest"))
    console.print(format_summary(summary))
    if violations:
        console.print(f"[yellow]风控干预 {len(violations)} 次（单票上限 "
                      f"{float(cfg['risk']['max_position_pct']):.0%}）[/yellow]")

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    equity.to_frame("equity").to_csv(out_dir / f"equity_{args.strategy}_{stamp}.csv")
    if not trades_df.empty:
        trades_df.to_csv(out_dir / f"trades_{args.strategy}_{stamp}.csv", index=False)
    with open(out_dir / f"summary_{args.strategy}_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    console.print(f"[green]结果已保存到 {out_dir}/[/green]")

    # 制品：供回溯与可复现性校验
    run.put_table("equity", equity.to_frame("equity"))
    if not trades_df.empty:
        run.put_table("trades", trades_df)
    run.put_json("summary", summary)
    run.add_metrics(
        **{k: summary.get(k) for k in
           ("cagr", "sharpe", "max_drawdown", "total_return", "calmar", "bench_excess_cagr")},
        n_trades=len(trades_df),
        n_risk_violations=len(violations),
    )
    manifest = run.finish()
    console.print(
        f"[green]制品目录: artifacts/{manifest.run_id}/  "
        f"(指纹 {manifest.fingerprint[:12]}, 数据 "
        f"{manifest.data_version.get('symbols', 0)} 只 / 截至 "
        f"{manifest.data_version.get('last_date')})[/green]"
    )


if __name__ == "__main__":
    main()
