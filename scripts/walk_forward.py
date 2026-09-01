"""Walk-Forward Analysis 命令行入口。

用法
----
```powershell
# 固定参数的样本外滚动（检验稳健性，不调参）
uv run python scripts/walk_forward.py --strategy lowvol_indz

# 每折在 train 段搜参数，再在 test 段验证
uv run python scripts/walk_forward.py --strategy lowvol_indz `
    --grid top_k=10,20,30 --grid rebalance_days=20,45

# 锚定窗口 + 更长隔离带
uv run python scripts/walk_forward.py --strategy lowvol_indz `
    --anchored --embargo 10 --train 756 --test 126
```

输出
----
* 控制台：逐折明细 + 样本外汇总 + 过拟合诊断
* `artifacts/wfa_{strategy}_{stamp}_*/`：manifest + 逐折表 + OOS 净值 + 汇总
* `reports/wfa_{strategy}_{stamp}.csv`：逐折明细（兼容现有前端）

判读
----
* `衰减比（OOS/IS）` ≥ 0.8：参数稳健
* 0.4 ~ 0.8：存在过拟合，实盘应打折预期
* < 0.4 或为负：参数选择基本在挑噪声，该策略不可用
* `参数一致率`：每折都选同一组参数才是真稳健
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import common
from quart.backtest.metrics import format_summary
from quart.backtest.walkforward import make_splits, run_walk_forward
from quart.config import load_config
from quart.data.artifacts import ArtifactStore
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_pit_universe, filter_for_simulation
from quart.risk.rules import make_weight_validator
from quart.strategy import build_strategy

console = Console()


def parse_grid(specs: list[str]) -> dict[str, list]:
    """--grid top_k=10,20,30 -> {"top_k": [10, 20, 30}]（自动推断数值类型）。"""
    grid: dict[str, list] = {}
    for spec in specs:
        key, _, raw = spec.partition("=")
        key, raw = key.strip(), raw.strip()
        if not key or not raw:
            raise SystemExit(f"--grid 格式应为 key=v1,v2,...，收到 {spec!r}")
        values = []
        for token in raw.split(","):
            token = token.strip()
            low = token.lower()
            if low in ("true", "false"):
                values.append(low == "true")
            elif low in ("none", "null"):
                values.append(None)
            else:
                try:
                    values.append(int(token))
                except ValueError:
                    try:
                        values.append(float(token))
                    except ValueError:
                        values.append(token)
        grid[key] = values
    return grid


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Walk-Forward 样本外验证")
    parser.add_argument("--strategy", default=cfg["strategy"]["name"])
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument(
        "--research-mode", choices=("exploratory", "formal"), default="exploratory",
        help="formal 强制按交易日 PIT 股票池；exploratory 标记为 NON_PIT",
    )
    parser.add_argument(
        "--universe-index", "--index", dest="universe_index",
        default=cfg["universe"]["default_index"],
        help="PIT 股票池指数代码（默认 config.universe.default_index）",
    )
    parser.add_argument("--train", type=int, default=504, help="训练窗口（交易日，默认 504≈2年）")
    parser.add_argument("--test", type=int, default=126, help="测试窗口（交易日，默认 126≈半年）")
    parser.add_argument("--step", type=int, default=None, help="滚动步长（默认=test）")
    parser.add_argument("--embargo", type=int, default=5, help="train/test 隔离天数（防泄漏）")
    parser.add_argument("--anchored", action="store_true", help="锚定起点，train 段逐折变长")
    parser.add_argument("--metric", default="sharpe", help="参数选择指标 sharpe/cagr/calmar")
    parser.add_argument("--grid", action="append", default=[], metavar="key=v1,v2")
    parser.add_argument("--min-trades", type=int, default=0, help="train 段最少成交笔数")
    parser.add_argument("--warmup", type=int, default=260, help="每折训练/测试前置历史窗口")
    parser.add_argument("--no-risk", action="store_true", help="关闭回测内风控")
    parser.add_argument(
        "--account-mode", choices=("continuous", "independent"), default="continuous",
        help="连续 OOS 账户（默认）或每折独立账户",
    )
    parser.add_argument("--save-dir", default=str(common.reports_dir()))
    args = parser.parse_args()

    store = BarStore()
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
        "rules_version": "ashare_v1",
        "rule_version": "ashare_v1",
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

    dc = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=dc.get("exclude_star", True),
        exclude_chinext=dc.get("exclude_chinext", True),
        exclude_st=dc.get("exclude_st", True),
        min_list_days=int(dc.get("min_list_days", 0)),
    )
    if bars.empty:
        raise SystemExit("过滤板块/ST 后无可用标的")
    universe_meta["coverage_start"] = str(pd.Timestamp(bars["date"].min()).date())
    universe_meta["coverage_end"] = str(pd.Timestamp(bars["date"].max()).date())

    md = MarketData.from_bars(bars, benchmark=bench)
    bench_close = bench.set_index("date")["close"].reindex(md.dates).ffill()

    grid = parse_grid(args.grid)
    base_params = dict(build_strategy(args.strategy).params)
    risk_pipeline = None if args.no_risk else make_weight_validator(
        float(cfg["risk"]["max_position_pct"])
    )

    splits = make_splits(
        len(md), args.train, args.test,
        step_days=args.step, embargo_days=args.embargo, anchored=args.anchored,
    )
    if not splits:
        need = args.train + args.embargo + args.test
        raise SystemExit(
            f"样本量不足：当前 {len(md)} 个交易日，"
            f"至少需要 {need} 个（train {args.train} + embargo {args.embargo} "
            f"+ test {args.test}）。\n"
            f"建议：缩短 --train/--test，或用 --start 放宽起始日期"
            f"（当前 {args.start}）。"
        )
    console.print(Panel(
        f"策略 {args.strategy} | {md.dates[0].date()} ~ {md.dates[-1].date()} "
        f"({len(md)} 交易日)\n"
        f"train={args.train} test={args.test} step={args.step or args.test} "
        f"embargo={args.embargo} {'锚定' if args.anchored else '滚动'} | "
        f"折数 {len(splits)}\n"
        f"参数网格: {grid or '无（固定参数前推）'} | 选择指标 {args.metric}",
        title="Walk-Forward Analysis",
    ))

    # 产出同时写 artifacts/（可追溯）与 reports/（兼容前端）
    run = ArtifactStore().create_run(
        f"wfa_{args.strategy}",
        params={
            "strategy": args.strategy, "grid": grid, "train_days": args.train,
            "test_days": args.test, "step_days": args.step,
            "embargo_days": args.embargo, "anchored": args.anchored,
            "metric": args.metric, "start": args.start, "end": args.end,
            "warmup_days": args.warmup,
            "risk_enabled": not args.no_risk,
            "account_mode": args.account_mode,
            "research_mode": args.research_mode,
            "universe": universe_meta,
        },
    )

    try:
        result = run_walk_forward(
            md, bench_close, args.strategy,
            param_grid=grid, base_params=base_params,
            train_days=args.train, test_days=args.test, step_days=args.step,
            embargo_days=args.embargo, anchored=args.anchored,
            selection_metric=args.metric,
            initial_cash=float(cfg["backtest"]["initial_cash"]),
            risk_pipeline=risk_pipeline,
            min_trades=args.min_trades,
            account_mode=args.account_mode,
            warmup_days=args.warmup,
            progress=lambda s: console.print(f"  {s}"),
        )
    except Exception as exc:
        run.finish(status="failed", error=str(exc))
        raise

    # ---------------- 逐折明细 ----------------
    detail = result.to_frame()
    table = Table(title="逐折明细：样本内选参 → 样本外验证")
    for col in detail.columns:
        table.add_column(str(col), justify="right" if col != "train" else "left")
    for _, r in detail.iterrows():
        table.add_row(*[
            f"{v:.3f}" if isinstance(v, float) else str(v) for v in r.tolist()
        ])
    console.print(table)

    # ---------------- 样本外汇总 ----------------
    console.print(Panel(format_summary(result.oos_summary), title="样本外（OOS）合成净值"))

    # ---------------- 过拟合诊断 ----------------
    decay = result.decay
    stability = result.param_stability
    n_active = result.n_folds_with_trades
    lines = []

    # 先提示"样本外根本没交易"：此时所有 OOS 指标恒为 0，
    # 直接报"严重过拟合"是误导——真实情况是窗口太短或流动性门槛太高。
    if n_active == 0:
        lines.append(
            f"⚠️ 全部 {len(result.folds)} 折在样本外均无成交 "
            f"（窗口 {args.test} 日过短，或流动性门槛/次新股过滤清空了候选池）。\n"
            f"   此时 OOS 指标恒为 0，下方衰减比无意义。请放长 --test 或放宽过滤。"
        )
    elif n_active < len(result.folds):
        lines.append(
            f"注意：{len(result.folds) - n_active}/{len(result.folds)} 折样本外无成交，"
            f"衰减比仅基于有成交的 {n_active} 折。"
        )

    if decay is None:
        lines.append("衰减比: 无法计算（无有效折、指标缺失或样本内指标均值非正）")
    else:
        verdict = (
            "参数稳健，样本外未明显衰减" if decay >= 0.8
            else "存在过拟合，实盘应打折预期" if decay >= 0.4
            else "严重过拟合：参数选择基本在挑噪声"
        )
        lines.append(f"衰减比 (OOS/IS {args.metric}): {decay:.2f}  → {verdict}")
    if stability:
        lines.append(
            "参数一致率: "
            + ", ".join(f"{k}={v:.0%}" for k, v in stability.items())
            + "  (1.0 = 每折选中同一组参数)"
        )
    else:
        lines.append("参数一致率: 无参数网格（固定参数前推）")
    console.print(Panel("\n".join(lines), title="过拟合诊断"))

    # ---------------- 落盘 ----------------
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.save_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out_dir / f"wfa_{args.strategy}_{stamp}.csv", index=False, encoding="utf-8-sig")

    run.put_table("folds", detail)
    run.put_table(
        "oos_equity",
        result.oos_equity.rename_axis("date").to_frame("equity").reset_index(),
    )
    run.put_json("oos_summary", result.oos_summary)
    run.add_metrics(
        decay=decay,
        param_stability=stability,
        n_folds=len(result.folds),
        n_folds_with_trades=n_active,
        oos_cagr=result.oos_summary.get("cagr"),
        oos_sharpe=result.oos_summary.get("sharpe"),
        oos_max_drawdown=result.oos_summary.get("max_drawdown"),
    )
    manifest = run.finish()

    console.print(f"[green]逐折明细: reports/wfa_{args.strategy}_{stamp}.csv[/green]")
    console.print(f"[green]制品目录: artifacts/{manifest.run_id}/  (指纹 {manifest.fingerprint})[/green]")
    console.print(
        "[dim]复现同参数结果时可比对 fingerprint；数据或配置变动后指纹会变，"
        "旧结论自动失效。[/dim]"
    )


if __name__ == "__main__":
    main()
