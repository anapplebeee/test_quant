from __future__ import annotations

from pathlib import Path

import pandas as pd

from quart.backtest.engine import FLAT, BaseStrategy, MarketData
from quart.config import PROJECT_ROOT
from quart.strategy.filters import apply_liquidity


class MLRankStrategy(BaseStrategy):
    """Hold top-k symbols by external model score (e.g. Alpha158+LGBM walk-forward preds).

    Scores are refreshed offline by scripts/train_ml.py; this strategy only reads them,
    so research and runtime stay decoupled.
    """

    name = "ml_rank"

    def prepare(self, md: MarketData) -> None:
        self._md = md
        p = self.params
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_score = p.get("min_score")
        self.stale_days = int(p.get("stale_days", 35))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.min_price = p.get("min_price")
        self.use_regime = bool(p.get("use_regime_filter", True))
        self.regime_days = int(p.get("regime_filter_days", 20))
        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean()
            if md.benchmark_close is not None
            else None
        )

        path = Path(p.get("scores_path") or PROJECT_ROOT / "data" / "scores" / "preds.csv")
        if not path.exists():
            raise FileNotFoundError(f"scores file not found: {path}, run scripts/train_ml.py first")
        df = pd.read_csv(path, parse_dates=["datetime"], dtype={"instrument": str})
        wide = df.pivot_table(index="datetime", columns="instrument", values="score", aggfunc="last").sort_index()

        dates = md.dates
        aligned = wide.reindex(wide.index.union(dates)).sort_index().ffill(limit=self.stale_days)
        self.scores = aligned.reindex(dates)
        self._next_rebalance = 0

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        row = self.scores.iloc[i].dropna() if i < len(self.scores) else pd.Series(dtype=float)
        if row.empty:
            return {}

        if self.use_regime and self.regime_ma is not None:
            bench_now = md.benchmark_close.iloc[i]
            ma_now = self.regime_ma.iloc[i]
            if pd.isna(bench_now) or pd.isna(ma_now) or bench_now < ma_now:
                return {FLAT: 1.0}

        if self.min_score is not None:
            row = row[row > float(self.min_score)]
            if row.empty:
                return {}

        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        row = row.loc[row.index.intersection(tradable)]
        row = apply_liquidity(row, md, i, self.min_avg_amount, self.liquidity_days, self.min_price)
        if row.empty:
            return {}

        top = row.nlargest(self.top_k)
        weight = min(1.0 / len(top), self.max_weight)
        return {sym: weight for sym in top.index}
