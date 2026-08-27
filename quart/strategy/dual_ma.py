from __future__ import annotations

import pandas as pd

from quart.backtest.engine import BaseStrategy, MarketData


class DualMAStrategy(BaseStrategy):
    """Long symbols whose fast SMA is above slow SMA; equal weight, capped count."""

    name = "dual_ma"

    def prepare(self, md: MarketData) -> None:
        self._md = md
        p = self.params
        self.fast = int(p.get("fast_days", 5))
        self.slow = int(p.get("slow_days", 20))
        self.max_names = int(p.get("max_names", 10))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.rebalance_days = int(p.get("rebalance_days", 1))
        self.warmup = self.slow + 1
        self.fast_ma = md.closes.rolling(self.fast).mean()
        self.slow_ma = md.closes.rolling(self.slow).mean()
        self._next_rebalance = self.warmup

    def target_weights(self, i: int) -> dict[str, float]:
        if i < self.warmup or i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days
        fast_row = self.fast_ma.iloc[i]
        slow_row = self.slow_ma.iloc[i]
        volume_row = self._md.volumes.iloc[i]
        active = [
            sym
            for sym in fast_row.index
            if not pd.isna(fast_row[sym])
            and not pd.isna(slow_row[sym])
            and fast_row[sym] > slow_row[sym]
            and (pd.isna(volume_row.get(sym)) is False and volume_row.get(sym, 0) > 0)
        ]
        if not active:
            return {}
        active = sorted(active, key=lambda s: fast_row[s] / slow_row[s] - 1, reverse=True)[: self.max_names]
        weight = min(1.0 / len(active), self.max_weight)
        return {sym: weight for sym in active}
