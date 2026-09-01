"""日频回测的成交价场景。

信号在 T 日收盘生成，成交发生在 T+1。open/vwap/close 都是对 T+1 日内执行方式
的显式假设：VWAP 与 close 使用当日完整 bar，只可作为回测成交场景，绝不能在
盘前实盘信号中当作已知报价。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quart.data.market import MarketData

PRICE_MODES = ("open", "vwap", "close")


@dataclass(frozen=True, slots=True)
class PriceScenarioResult:
    prices: pd.Series
    mode: str
    fallback_count: int = 0


def resolve_execution_prices(md: MarketData, i: int, mode: str = "open") -> PriceScenarioResult:
    """取得第 ``i`` 个执行日的价格基准，并明确记录 VWAP 回退数量。"""
    normalized = str(mode).strip().lower()
    if normalized not in PRICE_MODES:
        raise ValueError(f"未知 execution price mode {mode!r}，可用: {list(PRICE_MODES)}")
    if normalized == "open":
        return PriceScenarioResult(md.opens.iloc[i], normalized)
    if normalized == "close":
        return PriceScenarioResult(md.closes.iloc[i], normalized)

    typical = (md.highs.iloc[i] + md.lows.iloc[i] + md.closes.iloc[i]) / 3.0
    if md.amounts is None:
        return PriceScenarioResult(typical, normalized, fallback_count=int(typical.notna().sum()))
    volume = md.volumes.iloc[i]
    raw_vwap = md.amounts.iloc[i] / (volume * 100.0).replace(0, np.nan)
    low = md.lows.iloc[i]
    high = md.highs.iloc[i]
    valid = (
        raw_vwap.notna()
        & np.isfinite(raw_vwap)
        & low.notna()
        & high.notna()
        & raw_vwap.ge(low)
        & raw_vwap.le(high)
    )
    prices = raw_vwap.where(valid, typical)
    return PriceScenarioResult(prices, normalized, fallback_count=int((~valid & typical.notna()).sum()))


__all__ = ["PRICE_MODES", "PriceScenarioResult", "resolve_execution_prices"]
