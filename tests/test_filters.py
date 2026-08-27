from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import MarketData
from quart.strategy.filters import apply_liquidity


def make_md_with_amounts(n_days=30, symbols=("A", "B")) -> MarketData:
    dates = pd.date_range("2024-01-01", periods=n_days)
    frames = []
    amounts_map = {"A": 1e8, "B": 1e6}
    for s in symbols:
        prices = np.full(n_days, 10.0)
        frames.append(pd.DataFrame({
            "date": dates, "symbol": s, "open": prices, "high": prices,
            "low": prices, "close": prices,
            "volume": 1e6, "amount": amounts_map[s],
        }))
    bars = pd.concat(frames, ignore_index=True)
    return MarketData.from_bars(bars)


def test_liquidity_filters_low_turnover():
    md = make_md_with_amounts()
    scores = pd.Series({"A": 0.5, "B": 0.9})
    i = len(md.dates) - 1

    filtered = apply_liquidity(scores, md, i, min_avg_amount=50_000_000, days=20)
    assert list(filtered.index) == ["A"]

    unfiltered = apply_liquidity(scores, md, i, min_avg_amount=None, days=20)
    assert set(unfiltered.index) == {"A", "B"}

    strict = apply_liquidity(scores, md, i, min_avg_amount=5e9, days=20)
    assert strict.empty


def test_no_amount_data_disables_filter():
    md = make_md_with_amounts()
    md.amounts = None
    scores = pd.Series({"A": 0.5})
    out = apply_liquidity(scores, md, 10, min_avg_amount=5e7, days=20)
    assert list(out.index) == ["A"]
