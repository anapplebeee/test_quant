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


def test_group_z_industry_standardization(monkeypatch):
    """行业内 z-score：组内 (x-mean)/std，小样本组回退 NaN。"""
    import quart.strategy.industries as ind_mod

    monkeypatch.setattr(
        ind_mod, "load_industry_series",
        lambda level="first": pd.Series({"A": "I1", "B": "I1", "C": "I2", "D": "I2"}),
    )
    strat = LowVolCompositeStrategy()
    dates = pd.date_range("2024-01-01", periods=3)
    df = pd.DataFrame({"A": 1.0, "B": 3.0, "C": 10.0, "D": 20.0}, index=dates)

    out = strat._group_z(df, min_group_size=2)
    row = out.iloc[0]
    # 样本口径（ddof=1，与 _z() 全市场 z 一致）：[1,3] → mean 2, std √2 → z=∓0.7071
    assert np.isclose(row["A"], -1 / np.sqrt(2)) and np.isclose(row["B"], 1 / np.sqrt(2))
    assert np.isclose(row["C"], -1 / np.sqrt(2)) and np.isclose(row["D"], 1 / np.sqrt(2))

    # 组内样本不足 → NaN（当日剔除）
    out_small = strat._group_z(df, min_group_size=5)
    assert out_small.iloc[0].isna().all()


def test_indz_registry_default():
    from quart.strategy import build_strategy

    s = build_strategy("lowvol_indz", top_k=1)
    assert s.params.get("industry_z") is True
    s2 = build_strategy("lowvol_composite", top_k=1, industry_z=True)
    assert s2.params.get("industry_z") is True
    s3 = build_strategy("lowvol_composite", top_k=1)
    assert "industry_z" not in s3.params


def test_returns_empty_before_warmup():
    md = make_md()
    strat = LowVolCompositeStrategy(top_k=1, rebalance_days=5, use_regime_filter=False)
    strat.prepare(md)
    assert strat.target_weights(10) == {}
