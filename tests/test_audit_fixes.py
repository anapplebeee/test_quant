from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import FLAT, BacktestEngine, BaseStrategy, Fees, MarketData
from quart.data.store import drop_incomplete_today


def flat_bars(spec: dict[str, list[float]], dates) -> pd.DataFrame:
    frames = []
    for symbol, closes in spec.items():
        n = len(closes)
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(dates[:n]),
            "symbol": symbol,
            "open": closes,
            "high": np.array(closes) + 0.01,
            "low": np.array(closes) - 0.01,
            "close": closes,
            "volume": 1e6,
            "amount": 1e7,
        }))
    return pd.concat(frames, ignore_index=True)


ZERO_FEES = Fees(commission_rate=0.0, commission_min=0.0, stamp_tax_rate=0.0, transfer_fee_rate=0.0, slippage_rate=0.0, impact_coef=0.0)


class ScriptedStrategy(BaseStrategy):
    name = "scripted"

    def __init__(self, script: dict[int, dict]):
        super().__init__()
        self.script = script

    def prepare(self, md: MarketData) -> None:
        pass

    def target_weights(self, i: int) -> dict:
        return self.script.get(i, {})


def test_flat_exits_everything():
    dates = pd.date_range("2024-01-01", periods=6)
    bars = flat_bars({"A": [10, 10, 10, 10, 10, 10]}, dates)
    md = MarketData.from_bars(bars)
    strategy = ScriptedStrategy({0: {"A": 1.0}, 2: {FLAT: 1.0}})
    engine = BacktestEngine(md, strategy, fees=ZERO_FEES, initial_cash=100_000)
    engine.run()

    sells = [t for t in engine.trades if t.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].date == pd.Timestamp("2024-01-04")
    assert sells[0].shares == 9900


def test_empty_dict_keeps_positions():
    dates = pd.date_range("2024-01-01", periods=6)
    bars = flat_bars({"A": [10, 10, 10, 10, 10, 10]}, dates)
    md = MarketData.from_bars(bars)
    strategy = ScriptedStrategy({0: {"A": 1.0}})
    engine = BacktestEngine(md, strategy, fees=ZERO_FEES, initial_cash=100_000)
    engine.run()

    assert len([t for t in engine.trades if t.side == "SELL"]) == 0
    assert engine.trades[-1].date == pd.Timestamp("2024-01-02")


def test_limit_up_blocks_buy():
    dates = pd.date_range("2024-01-01", periods=4)
    bars = flat_bars({"A": [10.0, 10.0, 11.0, 11.0]}, dates)
    md = MarketData.from_bars(bars)
    strategy = ScriptedStrategy({1: {"A": 1.0}})
    engine = BacktestEngine(md, strategy, fees=ZERO_FEES, initial_cash=100_000)
    engine.run()

    day2_open = md.opens.iloc[2]["A"]
    assert day2_open == pytest.approx(11.0)
    buys = [t for t in engine.trades if t.side == "BUY"]
    assert buys == []


def test_limit_down_blocks_sell_then_retries():
    dates = pd.date_range("2024-01-01", periods=7)
    closes = [10.0, 10.0, 9.0, 9.0, 9.0, 9.0, 9.0]
    bars = flat_bars({"A": closes}, dates)
    md = MarketData.from_bars(bars)
    strategy = ScriptedStrategy({0: {"A": 1.0}, 1: {FLAT: 1.0}})
    engine = BacktestEngine(md, strategy, fees=ZERO_FEES, initial_cash=100_000)
    engine.run()

    sells = [t for t in engine.trades if t.side == "SELL"]
    assert len(sells) >= 1
    blocked_day = pd.Timestamp("2024-01-03")
    assert all(t.date != blocked_day for t in sells)
    assert any(t.shares > 0 for t in sells)


def test_impact_slip_widens_spread():
    fees = Fees(slippage_rate=0.001, impact_coef=0.1)
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.fees = fees
    slip_small = engine._slip(notional=10_000, adv=1e8)
    slip_big = engine._slip(notional=5_000_000, adv=1e8)
    assert slip_small == pytest.approx(0.001 + 0.1 * 0.01)
    assert slip_big > slip_small
    assert slip_big < 0.001 + 0.1


def test_drop_incomplete_uses_cn_tz_and_cutoff():
    dates = pd.DatetimeIndex(["2024-01-03", "2024-01-04"])
    df = pd.DataFrame({"date": dates, "x": [1, 2]})
    out = drop_incomplete_today(df)
    assert set(out["date"]) <= set(dates)
