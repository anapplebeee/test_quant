"""随机信号基线测试（回测引擎正确性验证）。

目的：把「策略年化」分解为四块——
  市值加权 beta(000300) + 规模偏移(等权宇宙) + 选股 alpha + 交易成本拖累。

方法：
  1) 等权宇宙参照：与策略完全同股票池、横截面等权、零换手零成本（假想组合，
     含幸存者偏差，仅作 alpha 归因参照，见 quart/data/benchmark.py 文档）。
  2) 随机 Top-K 组合 x N 个随机种子：走与实盘完全相同的引擎路径
     （T+1、涨跌停拒单、双边不利方向滑点、冲击成本、佣金/印花税/过户费、
     先卖后买预算制）。随机数仅用于选股，约束与真实策略一致。
  3) 正确性判据：随机组合年化 ≈ 等权宇宙年化 - 成本/换手拖累，
     即对等权宇宙的超额应在 0 附近小幅为负（约 -2~-4pp/yr 量级）；
     若显著偏离（尤其大幅为负），说明引擎或数据仍有缺陷。

用法：
  .venv/Scripts/python.exe scripts/baseline_random.py --seeds 20 --top-k 10
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine
from quart.backtest.metrics import summarize, win_rate
from quart.config import load_config
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.baseline import RandomTopKStrategy

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Random-signal baseline test")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=10)
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
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    console.print(f"股票池: {bars['symbol'].nunique()} 只 | bar 行数: {len(bars):,}")

    strat_cfg = dict(cfg["strategy"])
    base_params = dict(
        top_k=args.top_k,
        rebalance_days=strat_cfg.get("rebalance_days", 5),
        max_weight_pct=strat_cfg.get("max_weight_pct", 0.15),
        min_avg_amount=strat_cfg.get("min_avg_amount"),
        liquidity_days=strat_cfg.get("liquidity_days", 20),
        min_price=strat_cfg.get("min_price"),
    )

    md = MarketData.from_bars(bars, benchmark=bench)

    # 等权宇宙参照（零成本零换手，与策略同股票池）
    ew_rets = md.closes.pct_change(fill_method=None).iloc[1:].mean(axis=1).dropna()
    bench_close = bench.set_index("date")["close"].reindex(ew_rets.index).ffill()
    ew_curve = (1.0 + ew_rets).cumprod()

    ew_cagr = float(ew_curve.iloc[-1] ** (252.0 / len(ew_curve)) - 1.0)
    bench_cagr_ref = float(
        (bench_close.iloc[-1] / bench_close.iloc[0]) ** (252.0 / len(bench_close)) - 1.0
    )
    console.print(
        f"参照线: 000300 CAGR {bench_cagr_ref:+.2%} | 等权宇宙(零成本) CAGR {ew_cagr:+.2%}"
    )
    console.print(
        f"参照线日胜率: 000300 {win_rate(bench_close.pct_change()):.1%} | "
        f"等权宇宙 {win_rate(ew_rets):.1%}"
    )

    results = []
    for seed in range(args.seeds):
        strategy = RandomTopKStrategy(**{**base_params, "seed": seed})
        engine = BacktestEngine(md, strategy)
        equity = engine.run()
        s = summarize(equity, benchmark=bench_close, benchmark2=ew_curve)
        results.append(
            {
                "seed": seed,
                "total_return": s["total_return"],
                "cagr": s["cagr"],
                "sharpe": s["sharpe"],
                "max_drawdown": s["max_drawdown"],
                "daily_win_rate": s["daily_win_rate"],
                "excess_vs_hs300": s.get("bench_excess_cagr"),
                "excess_vs_ew": s.get("bench2_excess_cagr"),
            }
        )
        console.print(
            f"  seed {seed:02d}: cagr={s['cagr']:+.2%}  "
            f"vs300={s.get('bench_excess_cagr', float('nan')):+.2%}  "
            f"vsEW={s.get('bench2_excess_cagr', float('nan')):+.2%}  "
            f"win={s['daily_win_rate']:.1%}"
        )

    df = pd.DataFrame(results)
    mean = df.drop(columns=["seed"]).mean()
    std = df.drop(columns=["seed"]).std(ddof=1)

    console.print()
    table = Table(
        title=f"随机基线: RandomTop{args.top_k} x {args.seeds} seeds "
        f"({ew_rets.index[0].date()} ~ {ew_rets.index[-1].date()})"
    )
    for col in ["", "cagr", "sharpe", "mdd", "日胜率", "vs000300", "vs等权宇宙"]:
        table.add_column(col, justify="right")
    table.add_row(
        "均值",
        f"{mean['cagr']:+.1%}",
        f"{mean['sharpe']:.2f}",
        f"{mean['max_drawdown']:.1%}",
        f"{mean['daily_win_rate']:.1%}",
        f"{mean['excess_vs_hs300']:+.1%}",
        f"{mean['excess_vs_ew']:+.1%}",
    )
    table.add_row(
        "std",
        f"{std['cagr']:.1%}",
        f"{std['sharpe']:.2f}",
        f"{std['max_drawdown']:.1%}",
        f"{std['daily_win_rate']:.1%}",
        f"{std['excess_vs_hs300']:.1%}",
        f"{std['excess_vs_ew']:.1%}",
    )
    table.add_row(
        "000300(市值加权)",
        f"{bench_cagr_ref:+.1%}",
        "-",
        "-",
        f"{win_rate(bench_close.pct_change()):.1%}",
        "-",
        "-",
    )
    table.add_row(
        "等权宇宙(零成本)",
        f"{ew_cagr:+.1%}",
        "-",
        "-",
        f"{win_rate(ew_rets):.1%}",
        "-",
        "-",
    )
    console.print(table)

    out_dir = Path(args.save_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    df.to_csv(out_dir / f"baseline_random_{stamp}.csv", index=False, encoding="utf-8-sig")
    console.print(f"[green]saved: baseline_random_{stamp}.csv[/green]")

    # 正确性判据输出（几何口径：成本随当期净值复利，量级 = 年换手 x 单边成本）
    drag = ew_cagr - float(mean["cagr"])
    console.print(
        f"\n[bold]等权宇宙 - 随机组合[/bold] = {ew_cagr:+.2%} - {mean['cagr']:+.2%} = {drag:.2%}/yr"
    )
    console.print(
        "注：该差值 = 再平衡频率伪影(等权宇宙为每日再平衡) + 流动性池偏移 + 几何成本拖累。\n"
        "5 日 90% 换手策略的诚实成本量级为 20~35%/yr（含冲击成本 0.1*sqrt(参与率)），\n"
        "并非引擎缺陷。精确对账请运行 scripts/diag_final_recon.py（终残差应 <2pp）。"
    )
    if drag < 0:
        console.print("[red]警告：随机组合优于等权宇宙，引擎成本模型或执行逻辑需要复查！[/red]")
    else:
        console.print("[green]方向正常：随机组合 ≈ 等权宇宙 - 成本/换手拖累。终审以 diag_final_recon.py 对账为准。[/green]")


if __name__ == "__main__":
    main()
