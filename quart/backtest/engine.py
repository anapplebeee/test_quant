from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from quart.config import load_config


@dataclass
class Fees:
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_rate: float = 0.001

    @classmethod
    def from_config(cls) -> "Fees":
        cfg = load_config()["backtest"]
        return cls(
            commission_rate=cfg["commission_rate"],
            commission_min=cfg["commission_min"],
            stamp_tax_rate=cfg["stamp_tax_rate"],
            transfer_fee_rate=cfg["transfer_fee_rate"],
            slippage_rate=cfg["slippage_rate"],
        )

    def buy_cost(self, amount: float) -> float:
        commission = max(amount * self.commission_rate, self.commission_min)
        return commission + amount * self.transfer_fee_rate

    def sell_cost(self, amount: float) -> float:
        commission = max(amount * self.commission_rate, self.commission_min)
        return commission + amount * self.stamp_tax_rate + amount * self.transfer_fee_rate

    def buy_price(self, open_price: float) -> float:
        return open_price * (1 + self.slippage_rate)

    def sell_price(self, open_price: float) -> float:
        return open_price * (1 - self.slippage_rate)


class MarketData:
    def __init__(
        self,
        opens: pd.DataFrame,
        highs: pd.DataFrame,
        lows: pd.DataFrame,
        closes: pd.DataFrame,
        volumes: pd.DataFrame,
        benchmark_close: pd.Series | None = None,
        amounts: pd.DataFrame | None = None,
    ):
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.volumes = volumes
        self.close_val = closes.ffill()
        self.benchmark_close = benchmark_close
        self.amounts = amounts

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.opens.index

    @classmethod
    def from_bars(cls, bars: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> "MarketData":
        bars = bars.sort_values(["date", "symbol"])
        pivots = {
            name: bars.pivot_table(index="date", columns="symbol", values=name, aggfunc="last").sort_index()
            for name in ["open", "high", "low", "close", "volume", "amount"]
        }
        benchmark_close = None
        if benchmark is not None and not benchmark.empty:
            b = benchmark.sort_values("date").set_index("date")["close"]
            benchmark_close = b.reindex(pivots["close"].index)
        return cls(
            opens=pivots["open"],
            highs=pivots["high"],
            lows=pivots["low"],
            closes=pivots["close"],
            volumes=pivots["volume"],
            benchmark_close=benchmark_close,
            amounts=pivots.get("amount"),
        )


class BaseStrategy(ABC):
    name: str = "base"

    def __init__(self, **params):
        self.params = params

    def prepare(self, md: MarketData) -> None:
        pass

    @abstractmethod
    def target_weights(self, i: int) -> dict[str, float]:
        """Called at close of day i; returns {symbol: weight}. Empty dict means keep current."""


@dataclass
class Trade:
    date: pd.Timestamp
    symbol: str
    side: str
    shares: int
    price: float
    amount: float
    fee: float


LOT = 100
MIN_ORDER_VALUE = 1000.0


class BacktestEngine:
    def __init__(self, md: MarketData, strategy: BaseStrategy, fees: Fees | None = None, initial_cash: float | None = None):
        cfg = load_config()["backtest"]
        self.md = md
        self.strategy = strategy
        self.fees = fees or Fees.from_config()
        self.initial_cash = initial_cash if initial_cash is not None else cfg["initial_cash"]
        self.min_order_value = cfg.get("min_order_value", MIN_ORDER_VALUE)
        self.trades: list[Trade] = []

    def run(self) -> pd.Series:
        md = self.md
        self.strategy.prepare(md)
        dates = md.dates
        cash = self.initial_cash
        positions: dict[str, int] = {}
        equity_values: list[float] = []
        pending_targets: dict[str, float] | None = None

        for i in range(len(dates)):
            date = dates[i]
            if pending_targets is not None and i > 0:
                cash = self._rebalance(cash, positions, pending_targets, i)
            equity = cash + self._market_value(positions, i)
            equity_values.append(equity)
            pending_targets = self.strategy.target_weights(i) or None

        return pd.Series(equity_values, index=dates, name="equity")

    def _market_value(self, positions: dict[str, int], i: int) -> float:
        close_row = self.md.close_val.iloc[i]
        return sum(shares * close_row[sym] for sym, shares in positions.items() if shares > 0 and not pd.isna(close_row.get(sym)))

    def _rebalance(self, cash: float, positions: dict[str, int], targets: dict[str, float], i: int) -> float:
        md = self.md
        prev_close = md.close_val.iloc[i - 1]
        open_row = md.opens.iloc[i]
        equity_mark = cash + self._market_value(positions, i - 1)

        for sym in sorted(positions.keys()):
            weight = targets.get(sym, 0.0)
            shares = positions[sym]
            price_open = open_row.get(sym)
            if shares <= 0 or pd.isna(price_open):
                continue
            current_value = shares * prev_close.get(sym, price_open)
            desired_value = weight * equity_mark
            delta = desired_value - current_value
            if weight <= 0:
                sell_shares = shares
            elif delta < -self.min_order_value:
                px = self.fees.sell_price(price_open)
                sell_shares = min(shares, (abs(delta) // (px * LOT)) * LOT)
                sell_shares = int(sell_shares)
            else:
                continue
            if sell_shares <= 0:
                continue
            px = self.fees.sell_price(price_open)
            amount = sell_shares * px
            fee = self.fees.sell_cost(amount)
            cash += amount - fee
            remaining = shares - sell_shares
            if remaining > 0:
                positions[sym] = remaining
            else:
                del positions[sym]
            self.trades.append(Trade(date=md.dates[i], symbol=sym, side="SELL", shares=sell_shares, price=round(px, 4), amount=round(amount, 2), fee=round(fee, 2)))

        buy_syms = [s for s, w in sorted(targets.items(), key=lambda kv: -kv[1]) if w > 0]
        for sym in buy_syms:
            shares_held = positions.get(sym, 0)
            price_open = open_row.get(sym)
            if pd.isna(price_open):
                continue
            px = self.fees.buy_price(price_open)
            current_value = shares_held * prev_close.get(sym, price_open)
            desired_value = targets[sym] * equity_mark
            budget = min(desired_value - current_value, cash * 0.995)
            if budget < max(self.min_order_value, LOT * px):
                continue
            est_unit_cost = px * (1 + self.fees.commission_rate + self.fees.transfer_fee_rate)
            shares_to_buy = int((budget // (est_unit_cost * LOT)) * LOT)
            if shares_to_buy < LOT:
                continue
            amount = shares_to_buy * px
            fee = self.fees.buy_cost(amount)
            while shares_to_buy >= LOT and amount + fee > cash:
                shares_to_buy -= LOT
                amount = shares_to_buy * px
                fee = self.fees.buy_cost(amount)
            if shares_to_buy < LOT:
                continue
            cash -= amount + fee
            positions[sym] = shares_held + shares_to_buy
            self.trades.append(Trade(date=md.dates[i], symbol=sym, side="BUY", shares=shares_to_buy, price=round(px, 4), amount=round(amount, 2), fee=round(fee, 2)))

        return cash
