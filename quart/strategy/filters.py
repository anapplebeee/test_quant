from __future__ import annotations

import pandas as pd

from quart.backtest.engine import MarketData


def liquidity_mask(md: MarketData, i: int, min_avg_amount: float, days: int = 20) -> pd.Series | None:
    """Return boolean Series over symbols: rolling mean daily turnover >= threshold.

    None means no filter configured (or amount data unavailable).
    """
    if not min_avg_amount or md.amounts is None:
        return None
    lo = max(0, i - days + 1)
    avg = md.amounts.iloc[lo : i + 1].ffill().mean()
    return avg >= float(min_avg_amount)


def apply_liquidity(
    scores: pd.Series,
    md: MarketData,
    i: int,
    min_avg_amount: float,
    days: int = 20,
    min_price: float | None = None,
) -> pd.Series:
    mask = liquidity_mask(md, i, min_avg_amount, days)
    if mask is not None:
        eligible = mask[mask].index
        scores = scores.loc[scores.index.intersection(eligible)]
    if min_price is not None and md.close_val is not None:
        px = md.close_val.iloc[i]
        scores = scores.loc[scores.index.intersection(px[px >= float(min_price)].index)]
    return scores
