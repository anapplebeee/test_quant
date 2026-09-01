"""复合因子成本后组合回测（PROVISIONAL，研究口径）。

背景（2026-09-01 因子审计结论，见 docs 或 artifacts/factor_audit_*）：
    21 日 horizon 下三个通过审计门槛的新因子——
      speculative_crowding20_neg（IC 0.084、ICIR 0.63、recent 0.195）
      vwap_pos20_neg            （IC 0.087、ICIR 0.76、recent 0.127）
      rel_ind_rev20             （IC 0.066、ICIR 0.68、recent 0.123）
    彼此相关性 0.32~0.43，vwap 与其余两者仅 0.13~0.32，正交性结构良好。

本脚本回答"审计通过之后"的问题：等权 z-score 合成后在**真实成本口径**
（T+1 开盘执行、单边费率含佣金/印花税/过户/滑点）下是否仍有超额，以及
与现有低波因子（vol20_neg）合成是否互相增强。

口径：
    - 每 21 个交易日收盘生成信号（与审计 horizon 一致），次日开盘调仓；
    - rank top N 等权，无法成交（停牌/无开盘价）的剔除后重新归一；
    - 成本 = 单边换手 × 单边费率，双边收取；
    - PROVISIONAL：未含涨跌停拒买与逐笔流动性约束，正式准入走 WFA 门禁。

用法：
    .venv/Scripts/python.exe scripts/composite_backtest.py [--top 30] [--cost-bp 15]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from common import reports_dir
from quart.config import load_config
from quart.data.artifacts import STATUS_FAILED, ArtifactStore
from quart.data.market import MarketData
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.research.factor_audit import FACTOR_SPECS, FactorInputs

console = Console()

COMPOSITE_3 = ("crowding_liq20_neg", "vwap_pos20_neg", "rel_ind_rev20")
COMPOSITE_4 = (*COMPOSITE_3, "vol20_neg")


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """每日横截面百分位排名（0~1，并列取平均）。

    用 rank 而非 z-score：拥挤度计数类因子大量并列（如 20 日 0 次涨停），
    z-score 在并列段的微小噪声会被放大，导致选股在并列股间抖动、换手爆炸。
    """
    return frame.rank(axis=1, pct=True, method="average")


def build_composite(inputs: FactorInputs, names: tuple[str, ...]) -> pd.DataFrame:
    """等权合成：各因子横截面 rank 百分位后取均值。"""
    panels = []
    for name in names:
        panel = inputs.compute(name)
        if panel is None:
            raise RuntimeError(f"因子 {name} 无法计算（数据缺失？）")
        spec = next(s for s in FACTOR_SPECS if s.name == name)
        assert not spec.name.startswith("raw"), spec.name  # 全部因子已按越高越优定向
        panels.append(cross_sectional_rank(panel))
    stacked = pd.concat(panels, keys=names)
    return stacked.groupby(level=1).mean().astype("float32")


def rebalance_returns(
    composite: pd.DataFrame,
    opens: pd.DataFrame,
    *,
    top: int,
    period: int,
    warmup: int,
    cost_rate: float,
    liquidity: pd.DataFrame | None = None,
    min_amount: float = 20_000_000,
    buffer_mult: float = 1.5,
) -> tuple[pd.Series, pd.Series]:
    """Top-N 等权组合的逐期收益（开盘到开盘，T+1 执行，含成本）。

    - 流动性闸门：仅可在"20 日均成交额 ≥ min_amount"的股票中选股（与审计
      IC 口径一致）。缺失该过滤时低波/低拥挤因子会被停牌僵尸股占据——它们
      停牌期价格水平导致因子值"最优"，复牌补跌直接造成组合深亏。
    - buffer：持有股排名仍在前 top×buffer_mult 内则续持，仅补足空位，
      控制换手（与 lowvol_indz 的 rank_buffer 同思路）。
    """
    dates = composite.index
    positions = [i for i in range(warmup, len(dates) - 2, period)]
    period_returns: list[float] = []
    turnovers: list[float] = []
    prev_members: set[str] = set()

    for idx, i in enumerate(positions):
        signal_date = dates[i]
        next_trade = i + 1
        exit_trade = positions[idx + 1] + 1 if idx + 1 < len(positions) else len(dates) - 1
        if next_trade >= exit_trade:
            break
        scores = composite.loc[signal_date].dropna()
        if liquidity is not None:
            liq = liquidity.loc[signal_date].reindex(scores.index)
            scores = scores[liq >= min_amount]
        if len(scores) < top:
            continue
        ranked = scores.sort_values(ascending=False)

        keep_boundary = int(top * buffer_mult)
        members: list[str] = [s for s in prev_members if s in ranked.index[:keep_boundary]]
        for s in ranked.index:
            if len(members) >= top:
                break
            if s not in members:
                members.append(s)
        members_set = set(members[:top])

        entry = opens.iloc[next_trade]
        exit_ = opens.iloc[exit_trade]
        tradable = [
            s for s in members_set
            if pd.notna(entry.get(s)) and pd.notna(exit_.get(s)) and entry[s] > 0
        ]
        if not tradable:
            continue

        gross = float(np.mean([exit_[s] / entry[s] - 1.0 for s in tradable]))
        # 单边换手：新组合与旧组合的差异（首期按全建仓计）
        turnover = 1.0 if not prev_members else (
            len(members_set - prev_members) + len(prev_members - members_set)
        ) / 2 / max(len(members_set), 1)
        net = gross - turnover * cost_rate * 2  # 买卖双边
        period_returns.append(net)
        turnovers.append(turnover)
        prev_members = members_set

    index = [dates[min(p + 1, len(dates) - 1)] for p in positions[: len(period_returns)]]
    return pd.Series(period_returns, index=index, dtype="float64"), pd.Series(turnovers, index=index)


def perf_stats(returns: pd.Series, periods_per_year: float) -> dict[str, float]:
    if returns.empty:
        return {"cagr": np.nan, "sharpe": np.nan, "mdd": np.nan}
    equity = (1 + returns).cumprod()
    years = len(returns) / periods_per_year
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 and equity.iloc[-1] > 0 else np.nan
    vol = returns.std() * np.sqrt(periods_per_year)
    sharpe = (returns.mean() * periods_per_year) / vol if vol > 0 else np.nan
    mdd = float((equity / equity.cummax() - 1).min())
    return {"cagr": cagr, "sharpe": sharpe, "mdd": mdd}


def main() -> None:
    parser = argparse.ArgumentParser(description="Composite factor cost-aware backtest")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--period", type=int, default=21, help="调仓间隔（交易日）")
    parser.add_argument("--warmup", type=int, default=260)
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--cost-bp", type=float, default=15.0, help="单边费率（bps，含佣金+印花+过户+滑点）")
    args = parser.parse_args()

    writer = ArtifactStore().create_run("composite_backtest", vars(args))
    try:
        config = load_config()
        store = BarStore()
        bars = store.load(start=args.start, include_index=False)
        bars = filter_for_simulation(
            bars,
            exclude_star=bool(config["data"].get("exclude_star", True)),
            exclude_chinext=bool(config["data"].get("exclude_chinext", True)),
            exclude_st=bool(config["data"].get("exclude_st", True)),
            min_list_days=int(config["data"].get("min_list_days", 0)),
        )
        benchmark = store.load_benchmark(config["benchmark"])
        market = MarketData.from_bars(bars, benchmark)
        inputs = FactorInputs(market)
        cost_rate = args.cost_bp / 10_000
        ppy = 252 / args.period  # 每年调仓期数

        # 流动性闸门面板：20 日均成交额（与 factor_audit 的 min_amount 口径一致）
        liquidity = (
            inputs.amount.rolling(20).mean()
            if inputs.amount is not None and not np.all(np.isnan(inputs.amount))
            else None
        )

        portfolios: dict[str, pd.Series] = {}
        turnovers: dict[str, float] = {}

        for label, names in (
            ("composite3(拥挤+VWAP+行业反转)", COMPOSITE_3),
            ("composite4(+低波vol20)", COMPOSITE_4),
            ("vol20_neg(现策略代理)", ("vol20_neg",)),
        ):
            composite = build_composite(inputs, names)
            rets, turns = rebalance_returns(
                composite, market.opens, top=args.top, period=args.period,
                warmup=args.warmup, cost_rate=cost_rate,
                liquidity=liquidity,
            )
            portfolios[label] = rets
            turnovers[label] = float(turns.mean() * ppy) if not turns.empty else np.nan

        # 基准：按同一调仓节奏的收盘到收盘（近似开盘节奏，仅作参照）
        bench_close = market.benchmark_close if hasattr(market, "benchmark_close") else None
        if bench_close is not None:
            b = bench_close.reindex(portfolios["composite3(拥挤+VWAP+行业反转)"].index).pct_change().fillna(0)
            portfolios["沪深300(同节奏)"] = b

        table = Table(title=f"复合因子成本后回测（top{args.top} / {args.period}日调仓 / 单边{args.cost_bp:.0f}bp）")
        for col in ("组合", "CAGR", "Sharpe", "MaxDD", "年化单边换手"):
            table.add_column(col, justify="right")
        rows = {}
        for label, rets in portfolios.items():
            stats = perf_stats(rets, ppy)
            rows[label] = stats
            table.add_row(
                label,
                f"{stats['cagr']:+.2%}" if pd.notna(stats["cagr"]) else "-",
                f"{stats['sharpe']:.2f}" if pd.notna(stats["sharpe"]) else "-",
                f"{stats['mdd']:.1%}" if pd.notna(stats["mdd"]) else "-",
                f"{turnovers.get(label, float('nan')):.1f}x",
            )
        console.print(table)

        summary = pd.DataFrame(rows).T
        summary["annual_turnover"] = [turnovers.get(k) for k in summary.index]
        summary.to_csv(reports_dir() / "composite_backtest_summary.csv", encoding="utf-8-sig")
        for i, (label, rets) in enumerate(portfolios.items(), 1):
            safe = f"returns_{i}"
            writer.put_table(safe, rets.rename("ret").to_frame())
            writer.put_text(f"returns_{i}_label", label)
        writer.put_table("summary", summary.reset_index(names="portfolio"))
        manifest = writer.finish()
        console.print(f"[green]artifact[/green] {manifest.run_id}")
    except Exception as exc:
        writer.finish(status=STATUS_FAILED, error=str(exc))
        raise


if __name__ == "__main__":
    main()
