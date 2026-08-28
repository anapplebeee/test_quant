"""换手地板核算：调仓周期 5/10/15/20d × buffer 的成本几何分解。

每个组合重跑引擎，成本按几何口径分两块：
  fee_drag  = Σ ln(1 - fee_r / equity_r) / years          （佣金+印花税+过户费，Trade.fee）
  slip_drag = Σ ln(1 - slip_r / equity_r) / years          （滑点+冲击：成交价 vs 当日开盘）
并回答：每 1x 单边换手的总成本是多少 pp/yr → 换手地板在哪。
"""
from __future__ import annotations

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy import build_strategy

console = Console()
COMBOS = [
    {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 5},
    {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 10},
    {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 15},
    {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 20},
    {"top_k": 20, "rank_buffer": 0.0, "rebalance_days": 10},
]


def cost_decomp(engine: BacktestEngine, equity: pd.Series, cfg: dict) -> dict:
    tr = pd.DataFrame(
        {
            "date": [t.date for t in engine.trades],
            "symbol": [t.symbol for t in engine.trades],
            "side": [t.side for t in engine.trades],
            "amount": [float(t.amount) for t in engine.trades],
            "price": [float(t.price) for t in engine.trades],
            "fee": [float(t.fee) for t in engine.trades],
        }
    )
    tr["open_px"] = engine.md.opens.stack().reindex(pd.MultiIndex.from_arrays([tr["date"], tr["symbol"]])).values
    s = float(cfg["backtest"]["slippage_rate"])
    buy = tr["side"] == "BUY"
    # 买入多付：amount*(price/open - 1)；卖出少收：amount*(1 - price/open)
    tr["slip"] = tr["amount"] * ((tr["price"] / tr["open_px"] - 1).clip(lower=0)) * buy
    tr["slip"] += tr["amount"] * ((1 - tr["price"] / tr["open_px"]).clip(lower=0)) * (~buy)
    fee_d = tr.groupby("date")["fee"].sum()
    slip_d = tr.groupby("date")["slip"].sum()
    eq_d = equity.reindex(fee_d.index)
    years = len(equity) / 252.0
    fee_drag = float(np_logsum(fee_d, eq_d)) / years
    slip_drag = float(np_logsum(slip_d, eq_d)) / years
    return {"fee_drag": fee_drag, "slip_drag": slip_drag, "fee+slip": fee_drag + slip_drag}


def np_logsum(cost_d: pd.Series, eq_d: pd.Series) -> float:
    import numpy as np

    frac = (cost_d / eq_d).clip(upper=0.9, lower=0.0)
    return float(np.log1p(-frac).sum())


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2020-01-01")
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[bench["date"] >= "2020-01-01"]
    dc = cfg.get("data", {})
    filtered = filter_for_simulation(
        bars,
        exclude_star=dc.get("exclude_star", True),
        exclude_chinext=dc.get("exclude_chinext", True),
        exclude_st=dc.get("exclude_st", True),
        min_list_days=int(dc.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(filtered, benchmark=bench)
    base = {k: v for k, v in cfg["strategy"].items() if k != "name"}

    rows = []
    for combo in COMBOS:
        strat = build_strategy("lowvol_indz", **{**base, **combo})
        engine = BacktestEngine(md, strat, initial_cash=float(cfg["backtest"]["initial_cash"]))
        equity = engine.run()
        years = len(equity) / 252.0
        cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
        one_side = sum(float(t.amount) for t in engine.trades)
        to = one_side / 2.0 / float(equity.mean()) / years
        d = cost_decomp(engine, equity, cfg)
        rows.append(
            {
                "label": f"{combo['rebalance_days']}d buf={combo['rank_buffer']}",
                "cagr": cagr,
                "turnover": to,
                **d,
                "per_1x": (d["fee+slip"] / to) if to > 0 else float("nan"),
            }
        )
        console.print(f"  done: {rows[-1]['label']}  cagr={cagr:+.2%} turnover={to:.1f}x total_cost={d['fee+slip']:.1%}/yr")

    table = Table(title="换手地板核算（成本几何口径, 2020-01 ~ 2026-08, lowvol_indz top20）")
    for col in ("组合", "CAGR", "换手", "费用拖累", "滑点+冲击", "总成本/yr", "成本/1x换手"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            r["label"],
            f"{r['cagr']:+.1%}",
            f"{r['turnover']:.1f}x",
            f"{r['fee_drag']:.1%}",
            f"{r['slip_drag']:.1%}",
            f"{r['fee+slip']:.1%}",
            f"{r['per_1x']:.2%}",
        )
    console.print(table)
    pd.DataFrame(rows).to_csv("reports/turnover_floor_2026-08-28.csv", index=False, encoding="utf-8-sig")
    console.print("[green]saved: reports/turnover_floor_2026-08-28.csv[/green]")


if __name__ == "__main__":
    main()
