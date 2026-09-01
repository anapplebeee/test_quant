from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel

import common
from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import format_summary, summarize
from quart.config import load_config
from quart.data.artifacts import ArtifactStore
from quart.data.benchmark import equal_weight_benchmark
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_pit_universe, filter_for_simulation
from quart.execution.fees import Fees
from quart.risk.rules import make_weight_validator
from quart.strategy import build_strategy
from quart.strategy.parameters import (
    build_factor_receipt,
    core_strategy_overrides,
    parse_strategy_assignments,
)

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run A-share backtest")
    parser.add_argument("--strategy", default=load_config()["strategy"]["name"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--research-mode", choices=("exploratory", "formal"), default="exploratory",
        help="formal 强制按交易日 PIT 股票池；exploratory 标记为 NON_PIT",
    )
    parser.add_argument(
        "--universe-index", "--index", dest="universe_index",
        default=load_config()["universe"]["default_index"],
        help="PIT 股票池指数代码（默认 config.universe.default_index）",
    )
    parser.add_argument("--no-regime", action="store_true")
    parser.add_argument(
        "--regime-mode", choices=("ma", "score"), default=None,
        help="择时模式：ma=均线（默认），score=R4 多因子打分分级仓位",
    )
    parser.add_argument(
        "--timing-levels", type=int, default=None,
        help="score 模式档位数（2=全仓/空仓，3=加半仓档），仅 --regime-mode score 有效",
    )
    parser.add_argument(
        "--regime-filter-days", type=int, default=None,
        help="择时均线窗口（交易日），覆盖 config",
    )
    parser.add_argument(
        "--momentum-mode",
        choices=("simple", "rank", "smooth", "remove_limit_up"),
        default=None,
        help="动量口径：simple/rank/smooth/remove_limit_up",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=None,
        help="动量回看窗口（交易日），覆盖策略配置",
    )
    parser.add_argument(
        "--momentum-skip-days", type=int, default=None,
        help="动量跳过最近交易日数量（例如 20）",
    )
    parser.add_argument(
        "--limit-up-threshold", type=float, default=None,
        help="剔除涨停日动量的收益阈值（默认 0.095）",
    )
    parser.add_argument("--save-dir", default=str(common.reports_dir()))
    # 前端可调参数：显式传入时覆盖 config（含 config.strategy.overrides 按策略覆盖）
    parser.add_argument("--rebalance-days", type=int, default=None,
                        help="换手频率（交易日），覆盖 config")
    parser.add_argument("--top-k", type=int, default=None, help="持仓数量，覆盖 config")
    parser.add_argument("--rev-weight", type=float, default=None, help="短期反转因子权重，覆盖 config")
    parser.add_argument("--weight-mode", default=None, choices=["equal", "inv_vol", "zscore"],
                        help="组合权重模式（lowvol 系策略），覆盖 config")
    parser.add_argument("--vg-weight", type=float, default=None,
                        help="PIT 价值成长因子合成权重 0~1（lowvol 系策略），覆盖 config")
    parser.add_argument("--size-weight", type=float, default=None,
                        help="小市值因子权重（lowvol 系，需 baostock 基本面数据）")
    parser.add_argument("--turnover-weight", type=float, default=None,
                        help="低换手率因子权重（lowvol 系，需 baostock 基本面数据）")
    parser.add_argument("--value-weight", type=float, default=None,
                        help="价值因子权重 z(1/PE_TTM)（lowvol 系，需 baostock 基本面数据）")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="按策略 PARAMS_SCHEMA 传入高级参数；可重复使用，显式值优先",
    )
    parser.add_argument("--no-risk", action="store_true",
                        help="关闭回测内风控（默认启用，与实盘同一约束）")
    parser.add_argument(
        "--cost-multiplier",
        type=float,
        default=1.0,
        help="交易成本压力倍数，0=零成本、1=配置成本、2=双倍成本",
    )
    args = parser.parse_args()
    if not 0 <= args.cost_multiplier <= 10:
        parser.error("--cost-multiplier 必须在 0 到 10 之间")
    if args.rev_weight is not None and not 0 <= args.rev_weight <= 1:
        parser.error("--rev-weight 必须在 0 到 1 之间")

    cfg = load_config()
    store = BarStore()
    # 门禁必须看到原始输入；formal 不能靠“先排除 blocklist”绕过质量失败。
    from quart.data.quality import load_blocklist
    from quart.data.quality_gate import evaluate_quality_gate, require_quality_gate, save_quality_gate

    blocked = load_blocklist()
    bars = store.load(start=args.start, end=args.end)
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[(bench["date"] >= args.start) & (args.end is None or bench["date"] <= args.end)]
    if bars.empty:
        raise SystemExit("本地数据为空，请先运行 scripts/update_data.py")

    universe_meta = {
        "mode": args.research_mode,
        "index": args.universe_index,
        "quality": "PIT" if args.research_mode == "formal" else "NON_PIT",
        "source": "universe_history" if args.research_mode == "formal" else "all_local_bars",
        "rules_version": None,
        "rule_version": None,
    }
    if args.research_mode == "formal":
        try:
            bars = filter_for_pit_universe(bars, args.universe_index)
            from quart.data.pit_evidence import require_pit_evidence

            pit_evidence = require_pit_evidence(bars, index_code=args.universe_index)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if bars.empty:
            raise SystemExit("PIT 股票池在回测区间内为空")
        universe_meta["pit_evidence"] = pit_evidence.to_dict()

    quality_as_of = args.end or str(pd.Timestamp(bars["date"].max()).date())
    try:
        if args.research_mode == "formal":
            quality_gate = require_quality_gate(
                bars, bench, as_of=quality_as_of, blocked_symbols=blocked
            )
        else:
            quality_gate = evaluate_quality_gate(
                bars, bench, as_of=quality_as_of, blocked_symbols=blocked
            )
            save_quality_gate(quality_gate)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    universe_meta["quality_gate"] = quality_gate.to_dict()
    universe_meta["quality"] = (
        "FORMAL_PASS" if args.research_mode == "formal" else
        ("EXPLORATORY_PASS" if quality_gate.passed else "DEGRADED")
    )
    if not quality_gate.passed:
        console.print(
            f"[yellow]exploratory quality degraded: {len(quality_gate.issues)} issue(s); "
            "artifact 已标记 DEGRADED[/yellow]"
        )
    if blocked:
        console.print(f"[yellow]quality blocklist: excluding {len(blocked)} symbols[/yellow]")
        bars = bars[~bars["symbol"].astype(str).str.zfill(6).isin({str(s).zfill(6) for s in blocked})]

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
    universe_meta["coverage_start"] = str(pd.Timestamp(bars["date"].min()).date())
    universe_meta["coverage_end"] = str(pd.Timestamp(bars["date"].max()).date())

    explicit_params = {}
    if args.no_regime:
        explicit_params["use_regime_filter"] = False
    if args.regime_mode is not None:
        explicit_params["regime_mode"] = args.regime_mode
    if args.timing_levels is not None:
        explicit_params["timing_levels"] = args.timing_levels
    if args.regime_filter_days is not None:
        if args.regime_filter_days < 2:
            parser.error("--regime-filter-days 必须 ≥2")
        explicit_params["regime_filter_days"] = args.regime_filter_days
    if args.momentum_mode is not None:
        explicit_params["momentum_mode"] = args.momentum_mode
    if args.lookback_days is not None:
        if args.lookback_days < 1:
            parser.error("--lookback-days 必须为正整数")
        explicit_params["lookback_days"] = args.lookback_days
    if args.momentum_skip_days is not None:
        if args.momentum_skip_days < 0:
            parser.error("--momentum-skip-days 不能为负数")
        explicit_params["momentum_skip_days"] = args.momentum_skip_days
    if args.limit_up_threshold is not None:
        if not 0 < args.limit_up_threshold < 1:
            parser.error("--limit-up-threshold 必须在 0 到 1 之间")
        explicit_params["limit_up_threshold"] = args.limit_up_threshold
    try:
        explicit_params.update(core_strategy_overrides(
            args.strategy,
            rebalance_days=args.rebalance_days,
            top_k=args.top_k,
        ))
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    if args.rev_weight is not None:
        explicit_params["rev_weight"] = args.rev_weight
    if args.weight_mode is not None:
        explicit_params["weight_mode"] = args.weight_mode
    if args.vg_weight is not None:
        if not 0 <= args.vg_weight <= 1:
            parser.error("--vg-weight 必须在 0 到 1 之间")
        explicit_params["vg_weight"] = args.vg_weight
    for flag, key in (
        (args.size_weight, "size_weight"),
        (args.turnover_weight, "turnover_weight"),
        (args.value_weight, "value_weight"),
    ):
        if flag is not None:
            if flag < 0:
                parser.error(f"--{key.replace('_', '-')} 不能为负数")
            explicit_params[key] = flag
    try:
        explicit_params.update(parse_strategy_assignments(args.strategy, args.param))
    except (KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    strategy = build_strategy(args.strategy, **explicit_params)
    effective_params = dict(strategy.params)
    factor_request = build_factor_receipt(
        args.strategy, effective_params, source="request",
    )

    md = MarketData.from_bars(bars, benchmark=bench)
    # 风控进回测：默认与实盘同一约束，否则回测组合可以违反单票上限而实盘被截断
    violations: list[str] = []
    risk_pipeline = None
    if not args.no_risk:
        risk_pipeline = make_weight_validator(
            float(cfg["risk"]["max_position_pct"]), collect=violations
        )

    fees = Fees.from_config().scaled(args.cost_multiplier)
    execution_meta = {
        "price_model": "T+1 次日开盘价 + 不利方向滑点",
        "commission_rate": fees.commission_rate,
        "commission_min": fees.commission_min,
        "stamp_tax_rate": fees.stamp_tax_rate,
        "transfer_fee_rate": fees.transfer_fee_rate,
        "slippage_rate": fees.slippage_rate,
        "impact_coef": fees.impact_coef,
        "impact_model": "base + coef × sqrt(min(order/ADV5, 1))",
        "max_adv_participation": cfg["backtest"].get("max_adv_participation", 0.05),
        "capacity_model": "filled_notional <= ADV5 × max_adv_participation；超额延期",
        "lot_size": 100,
        "limit_rule": "按交易日与板块涨跌停规则拒单",
        "suspension_rule": "无开盘行情/不可交易时拒单，持仓继续估值",
        "price_adjust": data_cfg.get("adjust", "unknown"),
    }
    from quart.market_rules.rule_book import load_rule_book_version

    universe_meta["rules_version"] = load_rule_book_version()
    universe_meta["rule_version"] = universe_meta["rules_version"]
    execution_meta["rule_book_version"] = universe_meta["rules_version"]

    # 产出同时写 artifacts/（可追溯：run_id + 参数 + 数据版本 + 指纹）
    # 与 reports/（兼容现有 api/frontend）
    run = ArtifactStore().create_run(
        f"backtest_{args.strategy}",
        params={
            "strategy": args.strategy,
            **effective_params,
            "start": args.start, "end": args.end,
            "no_regime": args.no_regime,
            "risk_enabled": not args.no_risk,
            "cost_multiplier": args.cost_multiplier,
            "research_mode": args.research_mode,
            "universe": universe_meta,
            "execution": execution_meta,
            "factor_request": factor_request,
        },
    )

    from quart.data.security_master import MASTER_PATH, SecurityMaster

    security_master = SecurityMaster.load() if MASTER_PATH.exists() else None
    try:
        result = BacktestEngine(
            md,
            strategy,
            fees=fees,
            risk_pipeline=risk_pipeline,
            security_master=security_master,
        ).run_result()
    except Exception as exc:
        run.finish(status="failed", error=str(exc))
        raise

    equity = result.equity
    trades_df = result.trades
    deferred_df = result.deferred_orders

    bench_close = bench.set_index("date")["close"].reindex(equity.index).ffill()
    # 等权基准：与策略同股票池（已过滤板块/ST）的每日等权组合，衡量选股 alpha
    ew_bench = equal_weight_benchmark(equity, bars)
    summary = summarize(equity, benchmark=bench_close, benchmark2=ew_bench, benchmark2_name="bench2")
    factor_receipt = build_factor_receipt(
        args.strategy,
        effective_params,
        strategy=strategy,
        source="run",
    )
    portfolio_receipt = strategy.construction_receipt()
    summary.update({
        "strategy": args.strategy,
        "strategy_params": effective_params,
        "factor_receipt": factor_receipt,
        "initial_cash": result.initial_cash,
        "benchmark": cfg["benchmark"],
        "research_mode": args.research_mode,
        "universe": universe_meta,
        "execution": execution_meta,
        "rule_book_version": result.rule_book_version,
        "n_deferred_orders": len(deferred_df),
        "portfolio_construction": portfolio_receipt,
    })

    console.print(Panel(
        f"策略: {args.strategy}  | 交易笔数: {len(trades_df)} | "
        f"成本压力: {args.cost_multiplier:g}x | 股票池: {universe_meta['quality']}",
        title="Quart Backtest",
    ))
    console.print(format_summary(summary))
    if violations:
        console.print(f"[yellow]风控干预 {len(violations)} 次（单票上限 "
                      f"{float(cfg['risk']['max_position_pct']):.0%}）[/yellow]")

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    equity_frame = equity.to_frame("equity")
    bench_valid = bench_close.dropna()
    if not bench_valid.empty and float(bench_valid.iloc[0]) > 0:
        base = float(equity.iloc[0])
        equity_frame["benchmark"] = bench_close / float(bench_valid.iloc[0]) * base
        equity_frame["excess_nav"] = (
            equity / float(equity.iloc[0])
        ) / (bench_close / float(bench_valid.iloc[0]))
    equity_frame.to_csv(out_dir / f"equity_{args.strategy}_{stamp}.csv")
    if not trades_df.empty:
        trades_df.to_csv(out_dir / f"trades_{args.strategy}_{stamp}.csv", index=False)
    if not deferred_df.empty:
        deferred_df.to_csv(out_dir / f"deferred_{args.strategy}_{stamp}.csv", index=False)
    with open(out_dir / f"summary_{args.strategy}_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    console.print(f"[green]结果已保存到 {out_dir}/[/green]")

    # 制品：供回溯与可复现性校验
    run.put_table("equity", equity_frame.rename_axis("date").reset_index())
    if not trades_df.empty:
        run.put_table("trades", trades_df)
    if not deferred_df.empty:
        run.put_table("deferred_orders", deferred_df)
    run.put_json("summary", summary)
    run.put_json("factor_receipt", factor_receipt)
    if portfolio_receipt is not None:
        run.put_json("portfolio_construction", portfolio_receipt)
    run.add_metrics(
        **{k: summary.get(k) for k in
           ("cagr", "sharpe", "max_drawdown", "total_return", "calmar", "bench_excess_cagr")},
        n_trades=len(trades_df),
        n_deferred_orders=len(deferred_df),
        n_risk_violations=len(violations),
        n_enabled_factors=factor_receipt["enabled_count"],
        n_degraded_factors=factor_receipt["degraded_count"],
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
