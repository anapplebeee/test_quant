"""随机基线残差终审：几何成本口径对账 + 零费用引擎对照。

关键认知：成本按「当期净值的 ~0.6%/次调仓」计提，净值越跌基数越小，
简单求和(Σ成本/初始资金)会系统性低估 CAGR 口径的拖累。
正确恒等式：engine_CAGR ≈ twin_CAGR + Σ ln(1 - 成本_r/净值_r) / 年限

判据：
  1) 零费用引擎 vs 孪生参照：差值应≈0（仅剩涨跌停拒单/停牌冻结/整手取整效应）
  2) 全费用引擎 vs twin + 几何成本拖累：残差应≈0
两项都通过 => 引擎执行自洽，随机组合的 -20%/yr 完全由真实成本解释。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, FLAT, Fees, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation

from diag_random_decomp import RandomTopKStrategy

console = Console()


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2020-01-01")
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[bench["date"] >= "2020-01-01"]
    data_cfg = cfg.get("data", {})
    bars = filter_for_simulation(
        bars,
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(bars, benchmark=bench)
    closes = md.closes.ffill()
    n_days = len(md.dates)
    years = n_days / 252.0

    params = dict(
        top_k=10,
        rebalance_days=5,
        max_weight_pct=0.15,
        min_avg_amount=cfg["strategy"].get("min_avg_amount"),
        liquidity_days=cfg["strategy"].get("liquidity_days", 20),
        min_price=cfg["strategy"].get("min_price"),
    )
    free_fees = Fees(0, 0.0, 0, 0, 0, 0.0)  # 全零费用

    rows = []
    for seed in range(3):
        strat = RandomTopKStrategy(**{**params, "seed": seed})
        strat.prepare(md)
        picks_seq: dict[int, list[str]] = {}
        for i in range(n_days):
            w = strat.target_weights(i)
            if w and FLAT not in w:
                picks_seq[i] = list(w.keys())

        # 孪生参照（零成本，收盘进出）
        vals = [1.0] * n_days
        pos = 1.0
        seg_iter = iter(
            (i, min(i + 5, n_days - 1), float((closes[list(w)].iloc[min(i + 5, n_days - 1)] / closes[list(w)].iloc[i] - 1.0).mean()))
            for i, w in picks_seq.items()
        )
        cur = next(seg_iter, None)
        for d in range(n_days):
            if cur is not None and d >= cur[1]:
                pos *= 1.0 + cur[2]
                cur = next(seg_iter, None)
            vals[d] = pos
        twin = pd.Series(vals, index=md.dates)
        twin_cagr = float(twin.iloc[-1] ** (1 / years) - 1)

        # 引擎：全费用 + 零费用 各跑一次
        eng_full = BacktestEngine(md, RandomTopKStrategy(**{**params, "seed": seed}))
        eq_full = eng_full.run()
        full_cagr = float((eq_full.iloc[-1] / eq_full.iloc[0]) ** (1 / years) - 1)

        eng_free = BacktestEngine(md, RandomTopKStrategy(**{**params, "seed": seed}), fees=free_fees)
        eq_free = eng_free.run()
        free_cagr = float((eq_free.iloc[-1] / eq_free.iloc[0]) ** (1 / years) - 1)

        # 每个调仓日的成本占当期净值比例 -> 几何拖累
        tr = pd.DataFrame([t.__dict__ for t in eng_full.trades])
        opens = md.opens
        slip_yuan = []
        for t in eng_full.trades:
            op = float(opens.loc[t.date, t.symbol])
            slip_yuan.append(abs(t.price - op) * t.shares)
        tr["cost"] = tr["fee"].to_numpy() + np.array(slip_yuan)
        daily_cost = tr.groupby("date")["cost"].sum()
        eq_on_day = eq_full.reindex(daily_cost.index)
        frac = (daily_cost / eq_on_day).clip(upper=0.9)
        geom_drag = float(np.log1p(-frac).sum() / years)

        resid = full_cagr - twin_cagr - geom_drag
        exec_gap = free_cagr - twin_cagr
        rows.append(
            {
                "seed": seed,
                "twin": twin_cagr,
                "engine_free": free_cagr,
                "exec_gap": exec_gap,
                "engine_full": full_cagr,
                "geom_cost": geom_drag,
                "resid": resid,
            }
        )

    df = pd.DataFrame(rows)
    t = Table(title="终审对账（CAGR, /yr）")
    for c in ["", "twin零成本", "引擎0费", "执行缺口", "引擎全费", "几何成本拖累", "终残差"]:
        t.add_column(c, justify="right")
    for _, r in df.iterrows():
        t.add_row(
            f"seed{int(r['seed'])}",
            f"{r['twin']:+.1%}",
            f"{r['engine_free']:+.1%}",
            f"{r['exec_gap']:+.1%}",
            f"{r['engine_full']:+.1%}",
            f"{r['geom_cost']:+.1%}",
            f"{r['resid']:+.1%}",
        )
    m = df.drop(columns=["seed"]).mean()
    t.add_row(
        "均值",
        f"{m['twin']:+.1%}",
        f"{m['engine_free']:+.1%}",
        f"{m['exec_gap']:+.1%}",
        f"{m['engine_full']:+.1%}",
        f"{m['geom_cost']:+.1%}",
        f"{m['resid']:+.1%}",
    )
    console.print(t)
    pd.DataFrame(rows).to_csv("reports/diag_final_reconciliation.csv", index=False, encoding="utf-8-sig")
    console.print("[green]saved: reports/diag_final_reconciliation.csv[/green]")

    if abs(float(m["resid"])) <= 0.02 and abs(float(m["exec_gap"])) <= 0.03:
        console.print(
            f"[bold green]终审通过：执行缺口 {m['exec_gap']:+.1%}（涨跌停/停牌/整手效应，正常），"
            f"终残差 {m['resid']:+.1%} —— 引擎执行自洽，随机组合收益被 ~{abs(m['geom_cost']):.0%}/yr 的几何成本拖累完全解释。[/bold green]"
        )
    else:
        console.print(f"[red]仍有异常：执行缺口 {m['exec_gap']:+.1%} / 终残差 {m['resid']:+.1%}，继续排查。[/red]")


if __name__ == "__main__":
    main()
