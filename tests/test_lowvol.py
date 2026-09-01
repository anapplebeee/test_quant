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
    """行业内 z-score：组内 (x-mean)/std，小样本组回退全市场分。"""
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

    # 组内样本不足 → 回退输入的全市场复合分，避免映射缺失造成股票池骤缩
    out_small = strat._group_z(df, min_group_size=5)
    pd.testing.assert_frame_equal(out_small, df.astype("float32"))


def test_rank_buffer_syncs_actual_positions():
    strat = LowVolCompositeStrategy()
    strat.prepare(make_md())

    strat.sync_positions({"A": 0, "B": 100})

    assert strat._held == {"B"}


def test_indz_registry_default():
    from quart.strategy import build_strategy

    s = build_strategy("lowvol_indz", top_k=1)
    assert s.params.get("industry_z") is True
    s2 = build_strategy("lowvol_composite", top_k=1, industry_z=True)
    assert s2.params.get("industry_z") is True
    s3 = build_strategy("lowvol_composite", top_k=1)
    assert "industry_z" not in s3.params


def test_optional_robust_factors_are_disabled_until_research_admission():
    baseline = LowVolCompositeStrategy()
    assert baseline.params.get("long_vol_weight", 0.0) == 0.0
    assert baseline.params.get("downside_weight", 0.0) == 0.0
    assert baseline.params.get("tail_weight", 0.0) == 0.0

    candidate = LowVolCompositeStrategy(
        winsor_z=3.0,
        long_vol_weight=0.5,
        downside_weight=0.25,
        tail_weight=0.25,
        amount_stability_weight=0.2,
    )
    candidate.prepare(make_md(n_days=140))
    assert candidate.winsor_z == 3.0
    assert candidate.amount_stability_weight == 0.2
    assert not candidate.composite.empty


def test_event_limit_hit_exclusion_is_disabled_by_default_and_pit_when_enabled():
    dates = pd.bdate_range("2024-01-02", periods=45)
    symbols = ["600000", "600001"]
    close = pd.DataFrame(10.0, index=dates, columns=symbols)
    close.loc[dates[15], "600000"] = 11.0
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=symbols)
    md = MarketData(
        open_, close * 1.01, close * 0.99, close, volume, amounts=volume * close * 100
    )

    baseline = LowVolCompositeStrategy()
    baseline.prepare(md)
    assert baseline.event_eligible is None

    candidate = LowVolCompositeStrategy(event_max_limit_hits_20d=0)
    candidate.prepare(md)
    assert not bool(candidate.event_eligible.loc[dates[25], "600000"])
    assert bool(candidate.event_eligible.loc[dates[25], "600001"])


def test_limit_breadth_timing_uses_only_past_threshold_and_scales_exposure():
    dates = pd.bdate_range("2024-01-02", periods=90)
    symbols = [f"{600000 + index:06d}" for index in range(8)]
    close = pd.DataFrame(10.0, index=dates, columns=symbols)
    # 前半段周期性制造涨停，后半段不涨停，使低广度状态可识别。
    for position in range(5, 45, 5):
        close.loc[dates[position], symbols[:4]] = 11.0
        close.loc[dates[position + 1], symbols[:4]] = 10.0
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=symbols)
    md = MarketData(
        open_, close * 1.01, close * 0.99, close, volume, amounts=volume * close * 100
    )
    strategy = LowVolCompositeStrategy(
        top_k=2,
        rebalance_days=1,
        max_weight_pct=1.0,
        limit_breadth_timing=True,
        limit_breadth_window=20,
        limit_breadth_floor=0.5,
    )
    strategy.prepare(md)

    assert strategy.limit_breadth_exposure.loc[dates[-1]] == 0.5
    strategy.composite.iloc[-1] = np.arange(len(symbols), dtype=float)
    strategy.reversal = None
    weights = strategy.target_weights(len(dates) - 1)
    assert weights and sum(weights.values()) == 0.5


def test_event_only_candidate_uses_event_score_without_changing_default():
    md = make_md(n_days=80)
    baseline = LowVolCompositeStrategy()
    baseline.prepare(md)
    assert baseline.event_crowding_score is None

    candidate = LowVolCompositeStrategy(event_crowding_only=True, event_orthogonalize=True)
    candidate.prepare(md)
    assert candidate.event_crowding_score is not None
    pd.testing.assert_frame_equal(candidate.composite, candidate.event_crowding_score)


def test_returns_empty_before_warmup():
    md = make_md()
    strat = LowVolCompositeStrategy(top_k=1, rebalance_days=5, use_regime_filter=False)
    strat.prepare(md)
    assert strat.target_weights(10) == {}
