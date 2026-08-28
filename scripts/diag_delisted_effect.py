"""法证：182 只退市股入池为何让 lowvol_indz top20 改善（-4.9% -> -1.1%）？

三条通道隔离（均使用 lowvol_indz 同口径：industry_z=True）：
  C. 旧宇宙（3215 只，剔除退市）：验证新代码与旧 nlargest 路径等价（应复现 ≈-4.9%）
  A. 全宇宙 md + 退市股参与 z 统计但禁止选入（composite 直接删列）-> 隔离"z 统计污染"效应
  B. 全宇宙（3397 只）：完整效应（应 ≈-1.1%）
判读：
  A≈C 且 B>>A => 退市股被实际选入且贡献为正
  A≈B>>C => 三因子 z 配比漂移是主因（高波退市股膨胀 sigma_vol，改变因子相对权重）
"""
from __future__ import annotations

import pandas as pd
from rich.console import Console
from rich.table import Table

from quart.backtest.engine import BacktestEngine, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy.lowvol_composite import LowVolCompositeStrategy

console = Console()

BASE = dict(top_k=20, rank_buffer=0.0, industry_z=True)


class NoDelistedPickStrategy(LowVolCompositeStrategy):
    """退市股保留在 md/z 统计中，但 composite 中删列（不可被选入）。"""

    def prepare(self, md: MarketData) -> None:
        super().prepare(md)
        dl = set(self.params.get("delisted_symbols", ()))
        if dl:
            self.composite = self.composite.loc[:, ~self.composite.columns.isin(dl)]


def yearly(curve: pd.Series) -> pd.Series:
    g = curve.groupby(curve.index.year)
    return g.last() / g.first() - 1.0


def cagr(eq: pd.Series, years: float) -> float:
    return float((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)


def main() -> None:
    cfg = load_config()
    store = BarStore()
    bars_all = store.load(start="2020-01-01")
    bench = store.load_benchmark(cfg["benchmark"])
    bench = bench[bench["date"] >= "2020-01-01"]

    dl = pd.read_csv(store.universe_dir / "delisted.csv", dtype={"symbol": str})
    delisted = set(dl["symbol"].str.zfill(6))

    data_cfg = cfg.get("data", {})
    fkw = dict(
        exclude_star=data_cfg.get("exclude_star", True),
        exclude_chinext=data_cfg.get("exclude_chinext", True),
        exclude_st=data_cfg.get("exclude_st", True),
        min_list_days=int(data_cfg.get("min_list_days", 0)),
    )
    bars_full = filter_for_simulation(bars_all, **fkw)
    bars_old = filter_for_simulation(bars_all[~bars_all["symbol"].isin(delisted)], **fkw)
    console.print(f"全宇宙 {bars_full['symbol'].nunique()} | 旧宇宙 {bars_old['symbol'].nunique()}")

    md_full = MarketData.from_bars(bars_full, benchmark=bench)
    md_old = MarketData.from_bars(bars_old, benchmark=bench)
    years = len(md_full.dates) / 252.0
    base = {k: v for k, v in cfg["strategy"].items() if k != "name"}

    eq_c = BacktestEngine(md_old, LowVolCompositeStrategy(**{**base, **BASE})).run()
    eq_a = BacktestEngine(
        md_full, NoDelistedPickStrategy(**{**base, **BASE, "delisted_symbols": delisted})
    ).run()
    eq_b = BacktestEngine(md_full, LowVolCompositeStrategy(**{**base, **BASE})).run()

    t = Table(title="退市股效应隔离 (lowvol_indz top_k=20, buffer=0)")
    for c in ["场景", "CAGR", "2020", "2021", "2022", "2023", "2024", "2025", "2026"]:
        t.add_column(c, justify="right")
    for label, eq in (
        ("C 旧宇宙(等价验证)", eq_c),
        ("A 全宇宙/禁选退市", eq_a),
        ("B 全宇宙/完整", eq_b),
    ):
        yr = yearly(eq)
        row = [label, f"{cagr(eq, years):+.2%}"] + [
            f"{yr.get(int(y), float('nan')):+.1%}" for y in sorted(yr.index.astype(int))
        ]
        t.add_row(*row)
    console.print(t)


if __name__ == "__main__":
    main()
