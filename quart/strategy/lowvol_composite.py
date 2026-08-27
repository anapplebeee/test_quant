from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import BaseStrategy, MarketData
from quart.strategy.filters import apply_liquidity


class LowVolCompositeStrategy(BaseStrategy):
    """A-share low-anomaly composite: z(-vol20) + z(-amp20) + z(-lottery20).

    Research basis (scripts/factor_research.py, 2019-2026 full market):
    these three sibling factors hold |IC|~0.065 with stable halves both monthly and weekly.
    Optional short-reversal tilt via rev_weight.
    """

    name = "lowvol_composite"

    def _z(self, df: pd.DataFrame) -> pd.DataFrame:
        mu = df.mean(axis=1)
        sd = df.std(axis=1).replace(0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0).astype("float32")

    def prepare(self, md: MarketData) -> None:
        self._md = md
        p = self.params
        self.top_k = int(p.get("top_k", 10))
        self.rebalance_days = int(p.get("rebalance_days", 5))
        self.max_weight = float(p.get("max_weight_pct", 0.15))
        self.min_avg_amount = p.get("min_avg_amount")
        self.liquidity_days = int(p.get("liquidity_days", 20))
        self.use_regime = bool(p.get("use_regime_filter", False))
        self.regime_days = int(p.get("regime_filter_days", 20))
        self.rev_weight = float(p.get("rev_weight", 0.0))
        self.selection = str(p.get("selection", "composite"))

        c = md.close_val.astype("float32")
        ret1 = c.pct_change(fill_method=None)

        vol20 = -ret1.rolling(20).std().astype("float32")
        amp20 = (-((md.highs - md.lows) / md.closes.shift(1).replace(0, np.nan)).rolling(20).mean()).astype("float32")
        lotto = (-ret1.rolling(20).max()).astype("float32")

        z_vol = self._z(vol20)
        z_amp = self._z(amp20)
        z_lot = self._z(lotto)

        total = (z_vol.fillna(0) + z_amp.fillna(0) + z_lot.fillna(0))
        complete = z_vol.notna() & z_amp.notna() & z_lot.notna()
        comp = total / 3.0
        self.composite = comp.where(complete).astype("float32")
        del vol20, amp20, lotto, z_vol, z_amp, z_lot

        self.reversal = (-ret1.rolling(5).mean()).astype("float32")

        self.regime_ma = (
            md.benchmark_close.rolling(self.regime_days).mean() if md.benchmark_close is not None else None
        )
        self._next_rebalance = 0

    def target_weights(self, i: int) -> dict[str, float]:
        md = self._md
        if i < self._next_rebalance:
            return {}
        self._next_rebalance = i + self.rebalance_days

        if self.use_regime and self.regime_ma is not None:
            bench_now = md.benchmark_close.iloc[i]
            ma_now = self.regime_ma.iloc[i]
            if pd.isna(bench_now) or pd.isna(ma_now) or bench_now < ma_now:
                return {}

        scores = self.composite.iloc[i]
        if self.selection == "bounce":
            quiet = self.composite.iloc[i] >= self.composite.iloc[i].median(skipna=True)
            scores = scores.where(quiet)
            aligned = pd.DataFrame({"q": scores, "r": self.reversal.iloc[i]}).dropna()
            if aligned.empty:
                return {}
            scores = (aligned["q"] + aligned["r"]).astype("float32")
        elif self.reversal is not None:
            rev_z = self._z(self.reversal.iloc[i].to_frame().T).iloc[0]
            scores = scores.add(rev_z * self.rev_weight, fill_value=np.nan)

        scores = scores.dropna()
        volume_row = md.volumes.iloc[i]
        tradable = volume_row[volume_row.fillna(0) > 0].index
        scores = scores.loc[scores.index.intersection(tradable)]
        scores = apply_liquidity(scores, md, i, self.min_avg_amount, self.liquidity_days)
        if len(scores) < self.top_k:
            return {}

        top = scores.nlargest(self.top_k)
        weight = min(1.0 / len(top), self.max_weight)
        return {sym: weight for sym in top.index}
