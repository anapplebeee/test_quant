from __future__ import annotations

import pandas as pd

from quart.backtest.engine import BaseStrategy, MarketData
from quart.strategy.filters import apply_liquidity


class MomentumRotationStrategy(BaseStrategy):
    """Rank by N-day momentum, hold top-k equally weighted, rebalance every k days.

    Optional regime filter: go to cash when benchmark close is below its MA.
    """

    name = "momentum_rotation"

    def prepare(self, md: MarketData) -> None:
        self._md = md
        p = self.params
        self.lookback = int(p.get("lookback_days", 60))
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.industry_neutral = bool(p.get("industry_neutral", False))
        self.industry_level = str(p.get("industry_level", "first"))
        self.use_regime = bool(p.get("use_regime_filter", True))
        self.regime_days = int(p.get("regime_filter_days", 20))
        self.warmup = max(self.lookback, self.regime_days) + 1
        self.momentum = md.closes.pct_change(self.lookback)
        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean()
            if md.benchmark_close is not None
            else None
        )
        self._next_rebalance = self.warmup

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self.warmup or i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        if self.use_regime and self.regime_ma is not None:
            bench_now = md.benchmark_close.iloc[i]
            ma_now = self.regime_ma.iloc[i]
            if pd.isna(bench_now) or pd.isna(ma_now) or bench_now < ma_now:
                return {}

        scores = self.momentum.iloc[i].dropna()
        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        scores = scores.loc[scores.index.intersection(tradable)]
        scores = apply_liquidity(scores, md, i, self.min_avg_amount, self.liquidity_days)
        if self.industry_neutral:
            from quart.strategy.industries import industry_neutralize, load_industry_series
            try:
                industries = load_industry_series(self.industry_level)
            except FileNotFoundError:
                industries = pd.Series(dtype=object)
            scores = industry_neutralize(scores, industries)
        if len(scores) < self.top_k:
            return {}
        top = scores.nlargest(self.top_k)
        weight = min(1.0 / len(top), self.max_weight)
        return {sym: weight for sym in top.index}
