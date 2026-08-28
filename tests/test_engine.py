from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import BaseStrategy, BacktestEngine, Fees, MarketData


def make_bars(specs: dict[str, float], dates, step: float = 1.0) -> pd.DataFrame:
    frames = []
    for symbol, base_price in specs.items():
        n = len(dates)
        ramp = np.arange(n) * step
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(dates),
            "symbol": symbol,
            "open": base_price + ramp,
            "high": base_price + ramp + 0.5,
            "low": base_price + ramp - 0.5,
            "close": base_price + ramp,
            "volume": 1_000_000.0,
            "amount": (base_price + ramp) * 1_000_000.0,
        }))
    return pd.concat(frames, ignore_index=True)


ZERO_FEES = Fees(commission_rate=0.0, commission_min=0.0, stamp_tax_rate=0.0, transfer_fee_rate=0.0, slippage_rate=0.0)


class OnceStrategy(BaseStrategy):
    name = "once"

    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol

    def prepare(self, md: MarketData) -> None:
        self._fired = False

    def target_weights(self, i: int) -> dict[str, float]:
        if not getattr(self, "_fired", False):
            self._fired = True
            return {self.symbol: 1.0}
        return {}


class RotateStrategy(BaseStrategy):
    """day0: hold A; day>=2: hold B only."""

    def prepare(self, md: MarketData) -> None:
        pass

    def target_weights(self, i: int) -> dict[str, float]:
        if i < 2:
            return {"A": 1.0}
        return {"B": 1.0}


def test_no_lookahead_first_trade_on_next_open():
    dates = pd.date_range("2024-01-01", periods=10)
    bars = make_bars({"600001": 10.0}, dates, step=0.5)
    md = MarketData.from_bars(bars)
    engine = BacktestEngine(md, OnceStrategy("600001"), fees=ZERO_FEES, initial_cash=100_000)
    equity = engine.run()

    assert len(engine.trades) == 1
    t = engine.trades[0]
    assert t.date == pd.Timestamp("2024-01-02")
    assert t.price == pytest.approx(10.5)
    assert t.shares == 9400
    assert equity.iloc[-1] == pytest.approx(1300 + 9400 * 14.5)


def test_rotation_sells_then_buys_with_lots():
    dates = pd.date_range("2024-01-01", periods=6)
    bars = make_bars({"A": 10.0, "B": 10.0}, dates, step=0.0)
    md = MarketData.from_bars(bars)
    engine = BacktestEngine(md, RotateStrategy(), fees=ZERO_FEES, initial_cash=100_000)
    engine.run()

    sells = [t for t in engine.trades if t.side == "SELL"]
    buys = [t for t in engine.trades if t.side == "BUY"]
    assert any(t.symbol == "A" and t.shares == 9900 for t in sells)
    assert any(t.symbol == "B" and t.shares == 9900 for t in buys)
    b = next(t for t in buys if t.symbol == "B")
    assert b.date == pd.Timestamp("2024-01-04")


def test_t_plus_one_buy_not_sold_same_day():
    class FlipFlop(BaseStrategy):
        def prepare(self, md):
            pass

        def target_weights(self, i):
            return {"A": 1.0}

    dates = pd.date_range("2024-01-01", periods=8)
    bars = make_bars({"A": 20.0}, dates)
    md = MarketData.from_bars(bars)
    engine = BacktestEngine(md, FlipFlop(), fees=ZERO_FEES, initial_cash=50_000)
    engine.run()

    day_buys = {}
    day_sells = {}
    for t in engine.trades:
        (day_buys if t.side == "BUY" else day_sells).setdefault(t.date, 0)
        d = day_buys if t.side == "BUY" else day_sells
        d[t.date] += t.shares
    for date, bought in day_buys.items():
        prev_date = date - pd.Timedelta(days=1)
        sold_before = sum(sh for d, sh in day_sells.items() if d <= prev_date)
        total_bought_before = sum(sh for d, sh in day_buys.items() if d < date)
        assert bought <= total_bought_before - sold_before + bought


def test_momentum_strategy_runs_on_synthetic_panel():
    rng = np.random.default_rng(42)
    n_days, n_syms = 200, 15
    dates = pd.date_range("2023-01-02", periods=n_days)
    rets = pd.DataFrame(rng.normal(0.0005, 0.02, size=(n_days, n_syms)), index=dates,
                        columns=[f"S{i:03d}" for i in range(n_syms)])
    closes = (1 + rets).cumprod() * 10
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    bars = pd.DataFrame({
        "date": np.repeat(dates, n_syms),
        "symbol": np.tile(closes.columns.values, n_days),
        "open": opens.to_numpy().ravel(),
        "high": np.asarray(highs).ravel(),
        "low": np.asarray(lows).ravel(),
        "close": closes.to_numpy().ravel(),
        "volume": 1e6,
        "amount": 1e7,
    })
    bench = pd.DataFrame({"date": dates, "symbol": ["IDX000300"] * n_days,
                          "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3000.0,
                          "volume": 1e8, "amount": 3e11})
    md = MarketData.from_bars(bars, benchmark=bench)

    from quart.strategy.momentum import MomentumRotationStrategy

    strat = MomentumRotationStrategy(
        lookback_days=20, top_k=3, rebalance_days=5,
        max_weight_pct=0.35, use_regime_filter=False, regime_filter_days=20,
    )
    engine = BacktestEngine(md, strat, fees=ZERO_FEES, initial_cash=1_000_000)
    equity = engine.run()

    assert equity.notna().all()
    assert len(engine.trades) > 0
    assert (engine.trades[0].date > dates[20])
