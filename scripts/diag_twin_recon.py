"""随机基线残差精确核算（diag 第二步）。

方法：重放 RandomTopKStrategy 的 rng 序列取得完全相同的选股，
构建零成本孪生参照（信号日收盘价买入、持有 5 日、收盘价卖出、无费用滑点），
再与引擎结果逐项对账：
  引擎CAGR = 孪生参照CAGR - 显性费用 - 已实现滑点/冲击 - 执行时点漂移(残差)
若残差显著非零（>3pp/yr），说明引擎仍有系统性执行缺陷需排查。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, FLAT, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation

from diag_random_decomp import RandomTopKStrategy, yearly

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
    init_cash = float(cfg["backtest"]["initial_cash"])

    params = dict(
        top_k=10,
        rebalance_days=5,
        max_weight_pct=0.15,
        min_avg_amount=cfg["strategy"].get("min_avg_amount"),
        liquidity_days=cfg["strategy"].get("liquidity_days", 20),
        min_price=cfg["strategy"].get("min_price"),
    )

    rows = []
    for seed in range(3):
        # 1) 重放选股序列（与引擎内完全一致的 rng 消耗顺序）
        strat = RandomTopKStrategy(**{**params, "seed": seed})
        strat.prepare(md)
        picks_seq: dict[int, list[str]] = {}
        for i in range(n_days):
            w = strat.target_weights(i)
            if w and FLAT not in w:
                picks_seq[i] = list(w.keys())

        # 2) 零成本孪生参照：信号日收盘 i 买入、持有至 i+5 收盘卖出
        equity = 1.0
        curve = {}
        seg_rets = []
        for i, syms in picks_seq.items():
            j = min(i + 5, n_days - 1)
            r = (closes[syms].iloc[j] / closes[syms].iloc[i] - 1.0).mean()
            if not np.isfinite(r):
                continue
            seg_rets.append((i, j, float(r)))
        # 展开为日频净值（段内线性分摊，段间复利）
        vals = [1.0] * n_days
        pos = 1.0
        seg_iter = iter(seg_rets)
        cur = next(seg_iter, None)
        for d in range(n_days):
            if cur is not None and d >= cur[1]:
                pos *= 1.0 + cur[2]
                cur = next(seg_iter, None)
            vals[d] = pos
        twin = pd.Series(vals, index=md.dates)
        twin_cagr = float(twin.iloc[-1] ** (1 / years) - 1)

        # 3) 引擎实跑
        eng = BacktestEngine(md, RandomTopKStrategy(**{**params, "seed": seed}))
        eq = eng.run()
        eng_cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)

        # 4) 成本精确核算（trades 明细）
        tr = pd.DataFrame([t.__dict__ for t in eng.trades])
        tr["open_px"] = [float(md.opens.loc[t.date, t.symbol]) if t.symbol in md.opens.columns else np.nan for t in tr.itertuples()]
        tr["slip_frac"] = np.where(
            tr["side"] == "BUY", tr["price"] / tr["open_px"] - 1.0, 1.0 - tr["price"] / tr["open_px"]
        )
        fee_drag = float(tr["fee"].sum()) / init_cash / years
        slip_drag = float((tr["slip_frac"] * tr["amount"]).sum()) / init_cash / years

        gap = eng_cagr - twin_cagr
        residue = gap + fee_drag + slip_drag  # 引擎-参照 差额中未被费用/滑点解释的部分
        rows.append(
            {
                "seed": seed,
                "twin_cagr": twin_cagr,
                "engine_cagr": eng_cagr,
                "gap": gap,
                "fee_drag": -fee_drag,
                "slip_drag": -slip_drag,
                "residue": residue,
                "avg_slip_bp": float(tr["slip_frac"].mean()) * 1e4,
                "n_trades": len(tr),
            }
        )

    df = pd.DataFrame(rows)
    t = Table(title="随机策略 vs 零成本孪生参照（精确对账, /yr）")
    for c in ["", "twin零成本", "engine", "gap", "费用", "滑点/冲击", "残差", "单笔滑点bp"]:
        t.add_column(c, justify="right")
    for _, r in df.iterrows():
        t.add_row(
            f"seed{int(r['seed'])}",
            f"{r['twin_cagr']:+.1%}",
            f"{r['engine_cagr']:+.1%}",
            f"{r['gap']:+.1%}",
            f"{r['fee_drag']:+.1%}",
            f"{r['slip_drag']:+.1%}",
            f"{r['residue']:+.1%}",
            f"{r['avg_slip_bp']:.0f}",
        )
    m = df.drop(columns=["seed"]).mean()
    t.add_row(
        "均值",
        f"{m['twin_cagr']:+.1%}",
        f"{m['engine_cagr']:+.1%}",
        f"{m['gap']:+.1%}",
        f"{m['fee_drag']:+.1%}",
        f"{m['slip_drag']:+.1%}",
        f"{m['residue']:+.1%}",
        f"{m['avg_slip_bp']:.0f}",
    )
    console.print(t)

    out = pd.DataFrame(rows)
    out.to_csv("reports/diag_twin_reconciliation.csv", index=False, encoding="utf-8-sig")
    console.print("[green]saved: reports/diag_twin_reconciliation.csv[/green]")
    if abs(float(m["residue"])) > 0.03:
        console.print(f"[red]残差 {m['residue']:+.1%}/yr 超过 3pp 阈值 —— 引擎存在系统性执行缺陷，需继续排查！[/red]")
    else:
        console.print(f"[green]残差 {m['residue']:+.1%}/yr 在阈值内 —— 引擎执行与成本模型自洽，回测可信。[/green]")


if __name__ == "__main__":
    main()
