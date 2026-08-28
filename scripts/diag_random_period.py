"""20d 低频随机基线：检验 lowvol 20d 正收益是真选股 alpha 还是成本幻觉。

随机 Top20（无择时）× rebalance 5d/20d × 10 种子，与 lowvol_indz 同宇宙同成本。
干净对照：随机20d vs lowvol20d无择时（延伸 sweep 中 use_regime_filter=false 行）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, BaseStrategy, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy.filters import apply_liquidity

console = Console()


class RandomTopKStrategy(BaseStrategy):
    """每 rebalance_days 从流动性合格池随机等权抽取 top_k 只，不做任何择时。"""

    name = "random_topk20"

    def prepare(self, md: MarketData) -> None:
        self._md = md
        self.top_k = int(self.params.get("top_k", 20))
        self.rebalance_days = int(self.params.get("rebalance_days", 20))
        self.max_weight = float(self.params.get("max_weight_pct", 0.15))
        self.min_avg_amount = self.params.get("min_avg_amount")
        self.liquidity_days = int(self.params.get("liquidity_days", 20))
        self.min_price = self.params.get("min_price")
        self.warmup = self.liquidity_days + 1
        self._rng = np.random.default_rng(int(self.params.get("seed", 0)))
        self._next_rebalance = self.warmup

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self.warmup or i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days
        scores = pd.Series(0.0, index=md.closes.columns)
        volume_row = md.volumes.iloc[i]
        scores = scores.loc[volume_row[volume_row.fillna(0) > 0].index]
        scores = apply_liquidity(scores, md, i, self.min_avg_amount, self.liquidity_days, self.min_price)
        if len(scores) < self.top_k:
            return {}
        picks = self._rng.choice(scores.index.to_numpy(), size=self.top_k, replace=False)
        weight = min(1.0 / len(picks), self.max_weight)
        return {sym: weight for sym in picks}


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
    strat_cfg = cfg["strategy"]
    base = dict(
        max_weight_pct=strat_cfg.get("max_weight_pct", 0.15),
        min_avg_amount=strat_cfg.get("min_avg_amount"),
        liquidity_days=strat_cfg.get("liquidity_days", 20),
        min_price=strat_cfg.get("min_price"),
    )
    seeds = range(10)
    rows = []
    for period in (5, 20, 45):
        cagrs, tos, mdds = [], [], []
        for seed in seeds:
            strat = RandomTopKStrategy(**{**base, "top_k": 20, "rebalance_days": period, "seed": seed})
            engine = BacktestEngine(md, strat, initial_cash=float(cfg["backtest"]["initial_cash"]))
            equity = engine.run()
            years = len(equity) / 252.0
            cagrs.append((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
            one_side = sum(float(t.amount) for t in engine.trades)
            tos.append(one_side / 2.0 / float(equity.mean()) / years)
            dd = equity / equity.cummax() - 1
            mdds.append(float(dd.min()))
        rows.append(
            {
                "label": f"random Top20 {period}d",
                "cagr_mean": float(np.mean(cagrs)),
                "cagr_std": float(np.std(cagrs)),
                "turnover": float(np.mean(tos)),
                "mdd_mean": float(np.mean(mdds)),
            }
        )
        console.print(f"  done: {rows[-1]}")

    table = Table(title="同口径随机基线 vs lowvol（2020-01 ~ 2026-08）")
    for col in ("组合", "CAGR均值", "CAGR标准差", "换手", "MDD均值"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            r["label"],
            f"{r['cagr_mean']:+.1%}",
            f"±{r['cagr_std']:.1%}",
            f"{r['turnover']:.1f}x",
            f"{r['mdd_mean']:.1%}",
        )
    console.print(table)
    pd.DataFrame(rows).to_csv("reports/random_period_baseline_2026-08-28.csv", index=False, encoding="utf-8-sig")
    console.print("[green]saved: reports/random_period_baseline_2026-08-28.csv[/green]")


if __name__ == "__main__":
    main()
