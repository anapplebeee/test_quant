"""回测引擎。

职责边界（重要）
----------------
本模块**只**做三件事：

1. 时序驱动：按交易日推进，把策略在 T 日收盘的目标权重，延迟到 T+1 开盘执行
   （无未来函数的核心机制）。
2. 状态记账：维护 cash / positions / trades，产出净值曲线。
3. 装配执行上下文：把 MarketData 切片喂给 `quart.execution.generate_orders`。

**撮合与下单逻辑不在本模块**——它在 `quart/execution/`，且与实盘信号路径
(`quart/pipeline.py`) 共用同一实现。这是"回测结论可外推到实盘"的前提。

兼容性
------
`MarketData` / `BaseStrategy` / `Fees` / `FLAT` / `Trade` 等符号仍从本模块
导出（历史 import 路径全部保留），但实现已迁至各自的归属包。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quart.config import load_config
from quart.data.market import MarketData  # noqa: F401  (re-export)
from quart.execution.constraints import (  # noqa: F401  (re-export)
    A_SHARE_LOT as LOT,
)
from quart.execution.constraints import FLAT  # noqa: F401  (re-export)
from quart.execution.constraints import (
    limit_prices,  # noqa: F401  (re-export)
    price_limit_pct,  # noqa: F401  (re-export)
)
from quart.execution.fees import Fees  # noqa: F401  (re-export)
from quart.execution.models import BUY, ExecutionContext
from quart.execution.order_generator import generate_orders
from quart.strategy.base import BaseStrategy  # noqa: F401  (re-export)

MIN_ORDER_VALUE = 1000.0

#: 历史口径的买入现金垫。整手取整 + 最低佣金会吃掉小额现金，
#: 留 0.5% 缓冲避免"算得出但买不起"的循环回退。
LEGACY_CASH_BUFFER = 0.995

#: 冲击成本/流动性使用的 ADV 回看窗口（交易日）
ADV_WINDOW = 5


@dataclass
class Trade:
    date: pd.Timestamp
    symbol: str
    side: str
    shares: int
    price: float
    amount: float
    fee: float


@dataclass
class Portfolio:
    """账户状态。此前 cash/positions 是 `run()` 里的裸局部变量，
    无法快照、无法归因、无法在回测中途注入风控。"""

    cash: float
    positions: dict[str, int] = field(default_factory=dict)

    def market_value(self, prices: pd.Series) -> float:
        total = 0.0
        for sym, shares in self.positions.items():
            if shares <= 0:
                continue
            px = prices.get(sym, np.nan)
            if pd.isna(px):
                continue
            total += shares * float(px)
        return total

    def equity(self, prices: pd.Series) -> float:
        return self.cash + self.market_value(prices)


@dataclass
class BacktestResult:
    """回测完整产出。此前只有一条 equity Series，trades 要从
    `engine.trades` 侧信道取，各 script 各自拼装。"""

    equity: pd.Series
    trades: pd.DataFrame
    strategy: str
    params: dict
    initial_cash: float

    @property
    def final_positions(self) -> dict[str, int]:
        if self.trades.empty:
            return {}
        pos: dict[str, int] = {}
        for row in self.trades.itertuples():
            sign = 1 if row.side == BUY else -1
            pos[row.symbol] = pos.get(row.symbol, 0) + sign * int(row.shares)
        return {k: v for k, v in pos.items() if v > 0}


class BacktestEngine:
    """T+1 开盘撮合的日线回测引擎。

    Parameters
    ----------
    risk_pipeline:
        可选的风控钩子，在每次调仓前对目标权重做校验/归一化。
        默认 None（历史行为：风控不在回测内生效）。
        设为 `quart.risk.rules.validate_weights` 的包装即可让
        回测组合与实盘组合受同一约束。
    """

    def __init__(
        self,
        md: MarketData,
        strategy: BaseStrategy,
        fees: Fees | None = None,
        initial_cash: float | None = None,
        risk_pipeline=None,
    ):
        cfg = load_config()["backtest"]
        self.md = md
        self.strategy = strategy
        self.fees = fees or Fees.from_config()
        self.initial_cash = initial_cash if initial_cash is not None else cfg["initial_cash"]
        self.min_order_value = cfg.get("min_order_value", MIN_ORDER_VALUE)
        self.risk_pipeline = risk_pipeline
        self.trades: list[Trade] = []
        from quart.execution.backtest_model import BacktestExecutionModel

        self._model = BacktestExecutionModel(self.fees)

    # ---------------- 公开 API ----------------

    def reset(self) -> None:
        """清空运行态，使 `run()` 可重复调用（此前二次调用会重复追加 trades）。"""
        self.trades.clear()

    def run(self) -> pd.Series:
        """执行回测，返回净值曲线。"""
        return self.run_result().equity

    def run_result(self) -> BacktestResult:
        """执行回测，返回完整结果对象。"""
        self.reset()
        md = self.md
        self.strategy.prepare(md)
        dates = md.dates
        portfolio = Portfolio(cash=float(self.initial_cash))
        equity_values: list[float] = []
        pending_targets: dict[str, float] | None = None

        for i in range(len(dates)):
            if pending_targets is not None and i > 0:
                was_flat = bool(pending_targets.get(FLAT))
                portfolio.cash = self._rebalance(portfolio, pending_targets, i)
                if was_flat and portfolio.positions:
                    # 清仓未完成时（跌停/停牌），保持 FLAT 意图隔日继续挂单，
                    # 且不调用 target_weights——策略在空仓态不应再产出选股
                    pending_targets = {FLAT: 1.0}
                    equity_values.append(portfolio.equity(md.close_val.iloc[i]))
                    continue
                pending_targets = None

            equity_values.append(portfolio.equity(md.close_val.iloc[i]))
            raw = self.strategy.target_weights(i)
            if raw and FLAT in raw:
                pending_targets = {FLAT: 1.0}
            elif raw:
                pending_targets = raw
            else:
                pending_targets = None

        equity = pd.Series(equity_values, index=dates, name="equity")
        trades_df = pd.DataFrame([t.__dict__ for t in self.trades])
        return BacktestResult(
            equity=equity,
            trades=trades_df,
            strategy=getattr(self.strategy, "name", "unknown"),
            params=dict(getattr(self.strategy, "params", {})),
            initial_cash=float(self.initial_cash),
        )

    # ---------------- 内部实现 ----------------

    def _rebalance(self, portfolio: Portfolio, targets: dict[str, float], i: int) -> float:
        """在 T+1 开盘执行 target weights，返回执行后的现金。"""
        md = self.md
        prev_closes = md.close_val.iloc[i - 1]
        open_row = md.opens.iloc[i]
        adv = self._adv_row(i)

        equity_mark = portfolio.cash + portfolio.market_value(prev_closes)

        weights = self._apply_risk(targets, prev_closes, equity_mark)

        ctx = ExecutionContext(
            date=md.dates[i],
            targets=weights,
            equity=equity_mark,
            cash=portfolio.cash,
            positions=portfolio.positions,
            mark_prices=prev_closes,
            exec_prices=open_row,
            prev_closes=prev_closes,
            fees=self.fees,
            adv=adv,
            min_order_value=self.min_order_value,
            cash_buffer=LEGACY_CASH_BUFFER,
        )
        plan = generate_orders(ctx, self._model)
        portfolio.positions = dict(plan.ending_positions)

        for o in plan.orders:
            self.trades.append(
                Trade(
                    date=md.dates[i],
                    symbol=o.symbol,
                    side=o.side,
                    shares=o.shares,
                    price=round(o.exec_price, 4),
                    amount=round(o.amount, 2),
                    fee=round(o.fee, 2),
                )
            )
        return plan.ending_cash

    def _apply_risk(
        self,
        targets: dict[str, float],
        prices: pd.Series,
        equity: float,
    ) -> dict[str, float]:
        """风控前置。默认直通，注入 risk_pipeline 后与实盘同约束。"""
        if self.risk_pipeline is None or FLAT in targets:
            return targets
        return self.risk_pipeline(targets, prices, equity)

    def _adv_row(self, i: int) -> pd.Series | None:
        """近 ADV_WINDOW 日平均成交额（不含当日，避免前视）。"""
        md = self.md
        if md.amounts is None:
            return None
        lo = max(0, i - ADV_WINDOW)
        return md.amounts.iloc[lo:i].mean()


__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BaseStrategy",
    "FLAT",
    "Fees",
    "LOT",
    "MIN_ORDER_VALUE",
    "MarketData",
    "Portfolio",
    "Trade",
    "limit_prices",
    "price_limit_pct",
]
