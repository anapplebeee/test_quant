from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import BaseStrategy
from quart.backtest.stress import run_execution_stress
from quart.data.market import MarketData
from quart.execution.fees import Fees


class OnceStrategy(BaseStrategy):
    def prepare(self, md: MarketData) -> None:
        self.fired = False

    def target_weights(self, i: int) -> dict[str, float]:
        if self.fired:
            return {}
        self.fired = True
        return {"A": 1.0}


def _market() -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=5)
    bars = pd.DataFrame({
        "date": dates,
        "symbol": "A",
        "open": np.linspace(10, 11, len(dates)),
        "high": np.linspace(10.5, 11.5, len(dates)),
        "low": np.linspace(9.5, 10.5, len(dates)),
        "close": np.linspace(10.1, 11.1, len(dates)),
        "volume": 1_000_000.0,
        "amount": 1_000_000_000.0,
    })
    return MarketData.from_bars(bars)


def test_execution_stress_returns_complete_deterministic_grid():
    result = run_execution_stress(
        _market(),
        OnceStrategy,
        fees=Fees.zero(),
        initial_cash_values=[1_000_000, 100_000],
        cost_multipliers=[0.0, 1.0],
        execution_price_modes=["close", "open"],
        max_adv_participation=1.0,
    )

    assert len(result) == 8
    assert list(result.columns) == [
        "initial_cash", "cost_multiplier", "execution_price_mode", "cagr", "sharpe",
        "max_drawdown", "total_return", "n_trades", "trade_notional", "n_deferred_orders",
        "execution_price_fallbacks", "ending_equity",
    ]
    assert list(result["initial_cash"].unique()) == [100_000.0, 1_000_000.0]
    assert set(result["execution_price_mode"]) == {"open", "close"}
    assert (result["n_trades"] == 1).all()


@pytest.mark.parametrize("kwargs", [
    {"initial_cash_values": []},
    {"initial_cash_values": [0]},
    {"initial_cash_values": [1], "execution_price_modes": ["bad"]},
])
def test_execution_stress_rejects_invalid_grid(kwargs):
    with pytest.raises(ValueError):
        run_execution_stress(_market(), OnceStrategy, fees=Fees.zero(), **kwargs)
