from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import quart.strategy.factor_portfolio as factor_portfolio
from quart.backtest.engine import MarketData
from quart.portfolio import PortfolioConstructionContext
from quart.risk.exposure import ExposureSnapshot
from quart.strategy import build_strategy
from quart.strategy.factor_portfolio import FactorPortfolioStrategy


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


def test_factor_portfolio_routes_scores_through_constructor(monkeypatch):
    calls = []
    original = factor_portfolio.PortfolioConstructor.construct

    def spy(self, request, constraints):
        calls.append((request, constraints))
        return original(self, request, constraints)

    monkeypatch.setattr(factor_portfolio.PortfolioConstructor, "construct", spy)
    strategy = FactorPortfolioStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        min_cash_weight=0.1,
    )
    strategy.prepare(_market_data())

    weights = strategy.target_weights(50)
    receipt = strategy.construction_receipt()

    assert len(calls) == 1
    assert len(weights) == 3
    assert sum(weights.values()) == pytest.approx(0.9)
    assert max(weights.values()) <= 0.4 + 1e-10
    assert receipt is not None
    assert receipt["cash_weight"] == pytest.approx(0.1)
    assert receipt["target_weights"] == weights
    assert "position." + next(iter(weights)) in receipt["constraint_usage"]


def test_factor_portfolio_is_registered_and_receipt_is_a_factor_receipt():
    strategy = build_strategy("factor_portfolio", factor_names="vol20_neg")
    assert isinstance(strategy, FactorPortfolioStrategy)

    from quart.strategy.parameters import build_factor_receipt

    receipt = build_factor_receipt("factor_portfolio", strategy.params)
    assert receipt["is_factor_strategy"] is True
    assert receipt["formula"].endswith("PortfolioConstructor")


def test_factor_portfolio_fails_when_requested_factor_is_unavailable(monkeypatch):
    monkeypatch.setattr(factor_portfolio.FactorInputs, "compute", lambda self, name: None)
    strategy = FactorPortfolioStrategy(factor_names="size_neg")

    with pytest.raises(RuntimeError, match="不可用"):
        strategy.prepare(_market_data())


def test_factor_portfolio_passes_real_account_and_adv_context_to_constructor(monkeypatch):
    calls = []
    original = factor_portfolio.PortfolioConstructor.construct

    def spy(self, request, constraints):
        calls.append((request, constraints))
        return original(self, request, constraints)

    monkeypatch.setattr(factor_portfolio.PortfolioConstructor, "construct", spy)
    md = _market_data()
    strategy = FactorPortfolioStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
    )
    strategy.prepare(md)
    strategy.set_portfolio_context(PortfolioConstructionContext(
        date=md.dates[50],
        current_weights=pd.Series({"S0": 0.25}),
        equity=1_000_000.0,
        tradable=md.symbols,
        adv=pd.Series(100_000_000.0, index=md.symbols),
        max_adv_participation=0.05,
    ))

    strategy.target_weights(50)

    request, constraints = calls[0]
    assert request.current_weights["S0"] == pytest.approx(0.25)
    assert request.equity == 1_000_000.0
    assert request.adv is not None
    assert constraints.max_adv_participation == pytest.approx(0.05)


def test_factor_portfolio_uses_pit_exposure_snapshot_when_limits_requested():
    md = _market_data()
    snapshot = ExposureSnapshot(
        as_of=md.dates[50],
        available_at=md.dates[50],
        benchmark_weights=pd.Series(1 / len(md.symbols), index=md.symbols),
        market_caps=pd.Series(range(10, 10 + len(md.symbols)), index=md.symbols, dtype=float),
        source="test_pit",
        version="v1",
    )
    strategy = FactorPortfolioStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        market_cap_active_bound=10.0,
    )
    strategy.prepare(md)
    strategy.set_portfolio_context(PortfolioConstructionContext(
        date=md.dates[50], current_weights=pd.Series(dtype=float), equity=1.0,
        tradable=md.symbols, exposure_snapshot=snapshot,
    ))

    strategy.target_weights(50)

    assert strategy.last_construction is not None
    assert "market_cap.active" in strategy.last_construction.constraint_usage


def test_factor_portfolio_fails_closed_without_pit_exposure_snapshot():
    md = _market_data()
    strategy = FactorPortfolioStrategy(
        factor_names="vol20_neg", market_cap_active_bound=0.2,
    )
    strategy.prepare(md)
    strategy.set_portfolio_context(PortfolioConstructionContext(
        date=md.dates[50], current_weights=pd.Series(dtype=float), equity=1.0, tradable=md.symbols,
    ))

    with pytest.raises(RuntimeError, match="ExposureSnapshot"):
        strategy.target_weights(50)
