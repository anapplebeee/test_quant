"""近一年 / 近半年 收益与回撤实测：已验证配置 vs 沪深300 同期。

窗口口径与 metrics.WINDOWS 一致（交易日 126/252，含窗口首日为基期）。
配置取 2026-08-28 周期扫描的最优代表（reports/rebalance_period_2026-08-28.md）。
"""
from __future__ import annotations

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, MarketData
from quart.backtest.metrics import WINDOWS, max_drawdown, window_stats
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy import build_strategy

console = Console()

COMBOS = [
    ("lowvol_indz top30 45d", {"top_k": 30, "rank_buffer": 0.5, "rebalance_days": 45}),
    ("lowvol_indz top20 45d", {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 45}),
    ("lowvol_indz top20 20d", {"top_k": 20, "rank_buffer": 0.5, "rebalance_days": 20}),
]


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars = store.load(start="2020-01-01")
    bench_df = store.load_benchmark(cfg["benchmark"])
    bench_df = bench_df[bench_df["date"] >= "2020-01-01"].copy()
    bench_df["date"] = pd.to_datetime(bench_df["date"])
    bench = bench_df.set_index("date")["close"].astype(float).sort_index()

    dc = cfg.get("data", {})
    filtered = filter_for_simulation(
        bars,
        exclude_star=dc.get("exclude_star", True),
        exclude_chinext=dc.get("exclude_chinext", True),
        exclude_st=dc.get("exclude_st", True),
        min_list_days=int(dc.get("min_list_days", 0)),
    )
    md = MarketData.from_bars(filtered, benchmark=bench_df)
    base = {k: v for k, v in cfg["strategy"].items() if k != "name"}

    end = md.dates[-1].date()
    rows = []

    def add_row(label: str, eq: pd.Series) -> None:
        years = len(eq) / 252.0
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
        mdd_full, _ = max_drawdown(eq)
        row = {"组合": label, "全周期CAGR": f"{cagr:+.1%}", "全周期MDD": f"{mdd_full:+.1%}"}
        for wl, days in WINDOWS:
            ws = window_stats(eq, days)
            start = eq.index[-(days + 1)].date()
            row[f"{wl}收益"] = f"{ws['return']:+.1%}" if ws["return"] is not None else "-"
            row[f"{wl}MDD"] = f"{ws['mdd']:+.1%}" if ws["mdd"] is not None else "-"
            if wl == "last_1y":
                row["近1年区间"] = f"{start} ~ {end}"
        rows.append(row)

    for label, combo in COMBOS:
        strat = build_strategy("lowvol_indz", **{**base, **combo})
        engine = BacktestEngine(md, strat, initial_cash=float(cfg["backtest"]["initial_cash"]))
        eq = engine.run()
        add_row(label, eq)
        console.print(f"  done: {label}")

    add_row("沪深300（基准）", bench)

    table = Table(title=f"近1年/近半年收益与回撤（窗口截止 {end}）")
    cols = list(rows[0].keys())
    for c in cols:
        table.add_column(c, justify="right")
    for r in rows:
        table.add_row(*[str(r.get(c, "-")) for c in cols])
    console.print(table)
    pd.DataFrame(rows).to_csv("reports/recent_windows_2026-08-28.csv", index=False, encoding="utf-8-sig")
    console.print("[green]saved: reports/recent_windows_2026-08-28.csv[/green]")


if __name__ == "__main__":
    main()
