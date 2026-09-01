from __future__ import annotations

import pandas as pd
import pytest

from quart.backtest.engine import BacktestEngine, BaseStrategy, Fees, MarketData
from quart.execution.price_scenarios import resolve_execution_prices

ZERO_FEES = Fees(0.0, 0.0, 0.0, 0.0, 0.0)


class OnceStrategy(BaseStrategy):
    def prepare(self, md: MarketData) -> None:
        self.fired = False

    def target_weights(self, i: int) -> dict[str, float]:
        if self.fired:
            return {}
        self.fired = True
        return {"A": 1.0}


def _market() -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=3)
    bars = pd.DataFrame([
        {"date": dates[0], "symbol": "A", "open": 10.0, "high": 10.2, "low": 9.8,
         "close": 10.0, "volume": 1_000.0, "amount": 1_000_000.0},
        {"date": dates[1], "symbol": "A", "open": 10.0, "high": 12.0, "low": 9.0,
         "close": 10.8, "volume": 1_000.0, "amount": 1_050_000.0},
        {"date": dates[2], "symbol": "A", "open": 10.8, "high": 11.0, "low": 10.5,
         "close": 10.9, "volume": 1_000.0, "amount": 1_090_000.0},
    ])
    return MarketData.from_bars(bars)


@pytest.mark.parametrize("mode,expected", [("open", 10.0), ("vwap", 10.5), ("close", 10.8)])
def test_engine_uses_explicit_execution_price_scenario(mode, expected):
    result = BacktestEngine(
        _market(), OnceStrategy(), fees=ZERO_FEES, initial_cash=100_000,
        max_adv_participation=1.0, execution_price_mode=mode,
    ).run_result()

    assert result.trades.iloc[0]["price"] == pytest.approx(expected)
    assert result.execution_price_mode == mode
    assert result.execution_price_fallbacks == 0


def test_vwap_scenario_falls_back_to_typical_price_and_records_it():
    md = _market()
    md.amounts.iloc[1, 0] = 0.0
    md.volumes.iloc[1, 0] = 0.0

    scenario = resolve_execution_prices(md, 1, "vwap")

    assert scenario.prices["A"] == pytest.approx((12.0 + 9.0 + 10.8) / 3.0)
    assert scenario.fallback_count == 1


def test_unknown_execution_price_scenario_is_rejected():
    with pytest.raises(ValueError, match="execution price mode"):
        resolve_execution_prices(_market(), 1, "midnight")
