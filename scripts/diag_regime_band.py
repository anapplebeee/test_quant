"""决定性实验：14:57 旧 sweep (-4.94%) vs 当前 (-0.87%) 的差异是否来自择时迟滞带。

regime_band=0.0 → 朴素 MA 穿越（无 hysteresis，推测为旧代码行为）
regime_band=0.02 → 当前默认（迟滞带）
"""
import pandas as pd

from quart.backtest.engine import BacktestEngine, MarketData
from quart.config import load_config
from quart.data.store import BarStore
from quart.data.universe import filter_for_simulation
from quart.strategy.lowvol_composite import LowVolCompositeStrategy

cfg = load_config()
store = BarStore()
bars = store.load(start="2020-01-01")
bench = store.load_benchmark(cfg["benchmark"])
bench = bench[bench["date"] >= "2020-01-01"]
dl = pd.read_csv("data/universe/delisted.csv", dtype={"symbol": str})
delisted = set(dl["symbol"].str.zfill(6))
dc = cfg.get("data", {})
bars_old = filter_for_simulation(
    bars[~bars["symbol"].isin(delisted)],
    exclude_star=dc.get("exclude_star", True),
    exclude_chinext=dc.get("exclude_chinext", True),
    exclude_st=dc.get("exclude_st", True),
    min_list_days=int(dc.get("min_list_days", 0)),
)
md = MarketData.from_bars(bars_old, benchmark=bench)
years = len(md.dates) / 252.0
base = {k: v for k, v in cfg["strategy"].items() if k != "name"}

for band in (0.0, 0.02):
    eq = BacktestEngine(
        md, LowVolCompositeStrategy(**{**base, "top_k": 20, "rank_buffer": 0.0, "industry_z": True, "regime_band": band})
    ).run()
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    yr = eq.groupby(eq.index.year)
    yret = {k: f"{v:+.1%}" for k, v in (yr.last() / yr.first() - 1).round(3).items()}
    print(f"band={band}: cagr={cagr:+.2%} yearly={yret}")
