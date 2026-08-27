from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import MarketData
from quart.strategy.lowvol_composite import LowVolCompositeStrategy


def make_md(vol_a=0.01, vol_b=0.05, n_days=80) -> MarketData:
    dates = pd.date_range("2024-01-01", periods=n_days)
    rng = np.random.default_rng(3)

    def path(salt, vol):
        rets = rng.normal(0.0002, vol, size=n_days)
        return (1 + rets).cumprod() * 10

    closes = pd.DataFrame({"A": path(1, vol_a), "B": path(2, vol_b)}, index=dates)
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = np.maximum(opens, closes) * 1.005
    lows = np.minimum(opens, closes) * 0.995

    frames = []
    for s in ["A", "B"]:
        frames.append(pd.DataFrame({
            "date": dates, "symbol": s, "open": opens[s], "high": highs[s],
            "low": lows[s], "close": closes[s], "volume": 1e6, "amount": 1e8,
        }))
    bars = pd.concat(frames, ignore_index=True)
    return MarketData.from_bars(bars)


def test_prefers_calm_stock():
    md = make_md(vol_a=0.008, vol_b=0.05)
    strat = LowVolCompositeStrategy(
        top_k=1, rebalance_days=1, min_avg_amount=None,
        use_regime_filter=False, max_weight_pct=1.0,
    )
    strat.prepare(md)
    i = len(md.dates) - 2
    w = strat.target_weights(i)
    assert list(w.keys()) == ["A"]
    assert w["A"] == 1.0


def test_returns_empty_before_warmup():
    md = make_md()
    strat = LowVolCompositeStrategy(top_k=1, rebalance_days=5, use_regime_filter=False)
    strat.prepare(md)
    assert strat.target_weights(10) == {}
