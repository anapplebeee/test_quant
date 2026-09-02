from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import MarketData
from quart.execution.constraints import FLAT
from quart.strategy import build_strategy
from quart.strategy.three_layer import ThreeLayerStrategy


def _market_data() -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=80)
    rng = np.random.default_rng(19)
    closes: dict[str, np.ndarray] = {}
    for number in range(6):
        returns = rng.normal(0.0002, 0.008 + number * 0.002, len(dates))
        closes[f"S{number}"] = 10.0 * np.exp(np.cumsum(returns))
    close = pd.DataFrame(closes, index=dates)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    bars = []
    for symbol in close.columns:
        bars.extend(
            {
                "date": date,
                "symbol": symbol,
                "open": float(open_.loc[date, symbol]),
                "high": float(high.loc[date, symbol]),
                "low": float(low.loc[date, symbol]),
                "close": float(close.loc[date, symbol]),
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
            }
            for date in dates
        )
    return MarketData.from_bars(pd.DataFrame(bars))


def test_three_layer_is_registered_and_inherits_factor_portfolio():
    strategy = build_strategy("three_layer", factor_names="vol20_neg")
    assert isinstance(strategy, ThreeLayerStrategy)
    assert strategy.name == "three_layer"


def test_market_timing_off_has_same_shape_as_factor_portfolio():
    """market_timing=False 时 three_layer 退化为 factor_portfolio（不加额外历史）。"""
    strategy = ThreeLayerStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        market_timing=False,
    )
    assert strategy.required_history_days == 61  # 未启用择时：与 factor_portfolio 一致
    strategy.prepare(_market_data())
    weights = strategy.target_weights(50)
    assert len(weights) == 3
    assert sum(weights.values()) == pytest.approx(1.0)  # Constructor 归一化
    assert strategy._state_exposure is None


def test_risk_off_maps_to_flat():
    """大盘择时 risk_off（exposure=0）→ 目标权重整体清仓（FLAT）。"""
    strategy = ThreeLayerStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        market_timing=False,  # 手动注入 exposure，绕过真实 market_state_vector 数值路径
    )
    md = _market_data()
    strategy.prepare(md)
    # 伪造全市场 risk_off（exposure=0）：target_weights 应清仓
    strategy._state_exposure = pd.Series(0.0, index=md.dates)
    weights = strategy.target_weights(50)
    assert weights == {FLAT: 1.0}


def test_transition_scales_weights_down():
    """大盘择时 transition（exposure<1）→ 目标权重按 exposure 缩放（不清仓）。"""
    strategy = ThreeLayerStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        market_timing=False,
    )
    md = _market_data()
    strategy.prepare(md)
    # 伪造全市场 transition（exposure=0.5）
    strategy._state_exposure = pd.Series(0.5, index=md.dates)
    timed = strategy.target_weights(50)
    assert FLAT not in timed
    assert sum(timed.values()) == pytest.approx(0.5)
    assert max(timed.values()) <= 0.4 * 0.5 + 1e-9
