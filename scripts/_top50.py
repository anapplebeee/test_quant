import pandas as pd

from quart.config import load_config
from quart.data.store import BarStore
from quart.backtest.engine import MarketData

cfg = load_config()
store = BarStore()
asof = pd.Timestamp("2026-08-26")

bars = store.load()
bars = bars[bars["date"] <= asof]
bench = store.load_benchmark(cfg["benchmark"])
md = MarketData.from_bars(bars, benchmark=bench)

i = len(md.dates) - 1
closes = md.closes.ffill()

mom60 = closes.pct_change(lookback := 60).iloc[i]
mom20 = closes.pct_change(20).iloc[i]
vol_row = md.volumes.iloc[i]

valid = mom60.dropna()
tradable = vol_row[vol_row.fillna(0) > 0].index
scores = valid.loc[valid.index.intersection(tradable)].sort_values(ascending=False)

name_map = {}
try:
    import akshare as ak
    name_map = dict(ak.stock_info_a_code_name().values.tolist())
except Exception:
    from quart.data.source_akshare import fetch_stock_list
    try:
        name_map = dict(fetch_stock_list().values.tolist())
    except Exception:
        pass

print(f"=== 全市场动量排名 TOP50 @ {md.dates[i].date()} | 样本 {len(scores)} ===\n")
print(f"{'#':<3} {'代码':<7} {'名称':<8} {'收盘':>8} {'60日':>9} {'20日':>9} {'5日':>9} {'今日成交额(亿)':>12}")
m5_all = closes.pct_change(5).iloc[i]
amount_row = (md.volumes * closes).iloc[-1] / 1e8

for r, (code, v) in enumerate(scores.head(50).items(), 1):
    close = md.closes.iloc[i][code]
    nm = name_map.get(code, "")
    amt = amount_row.get(code, float("nan"))
    print(f"{r:<3} {code:<7} {nm:<8} {close:>8.2f} {v:>+8.1%} {mom20.get(code, float('nan')):>+8.1%} {m5_all.get(code, float('nan')):>+8.1%} {amt:>12.1f}")
