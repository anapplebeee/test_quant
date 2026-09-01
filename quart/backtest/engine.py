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
from quart.data.market import MarketData
from quart.execution.constraints import (
    A_SHARE_LOT as LOT,
)
from quart.execution.constraints import FLAT, limit_prices, price_limit_pct
from quart.execution.fees import Fees
from quart.execution.models import BUY, ExecutionContext
from quart.execution.order_generator import generate_orders
from quart.strategy.base import BaseStrategy

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
    ending_cash: float | None = None
    ending_positions: dict[str, int] = field(default_factory=dict)
    pending_targets: dict[str, float] | None = None
    strategy_state: dict = field(default_factory=dict)
    rule_book_version: str | None = None

    @property
    def final_positions(self) -> dict[str, int]:
        if self.ending_positions:
            return dict(self.ending_positions)
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
        signal_md: MarketData | None = None,
        signal_offset: int = 0,
        rule_book=None,
        security_master=None,
    ):
        cfg = load_config()["backtest"]
        self.md = md
        self.signal_md = md if signal_md is None else signal_md
        self.signal_offset = int(signal_offset)
        if self.signal_offset < 0 or self.signal_offset + len(md) > len(self.signal_md):
            raise ValueError("signal_offset 与 signal_md 长度不匹配")
        signal_dates = self.signal_md.dates[self.signal_offset : self.signal_offset + len(md)]
        if not signal_dates.equals(md.dates):
            raise ValueError("signal_md 的执行区间日期必须与 md 对齐")
        self.strategy = strategy
        self.fees = fees or Fees.from_config()
        self.initial_cash = initial_cash if initial_cash is not None else cfg["initial_cash"]
        self.min_order_value = cfg.get("min_order_value", MIN_ORDER_VALUE)
        self.risk_pipeline = risk_pipeline
        self.trades: list[Trade] = []
        from quart.execution.backtest_model import BacktestExecutionModel
        from quart.execution.rule_resolver import ExecutionRuleResolver

        self.rule_resolver = ExecutionRuleResolver(rule_book, security_master)
        self._model = BacktestExecutionModel(self.fees, rule_resolver=self.rule_resolver)

    # ---------------- 公开 API ----------------

    def reset(self) -> None:
        """清空运行态，使 `run()` 可重复调用（此前二次调用会重复追加 trades）。"""
        self.trades.clear()

    def run(self) -> pd.Series:
        """执行回测，返回净值曲线。"""
        return self.run_result().equity

    def run_result(
        self,
        initial_cash: float | None = None,
        initial_positions: dict[str, int] | None = None,
        pending_targets: dict[str, float] | None = None,
    ) -> BacktestResult:
        """执行回测，返回完整结果对象。"""
        self.reset()
        md = self.md
        signal_md = self.signal_md
        self.strategy.prepare(signal_md)
        dates = md.dates
        starting_cash = self.initial_cash if initial_cash is None else initial_cash
        portfolio = Portfolio(
            cash=float(starting_cash),
            positions={str(k): int(v) for k, v in (initial_positions or {}).items() if int(v) > 0},
        )
        equity_values: list[float] = []
        carried_targets = dict(pending_targets) if pending_targets else None

        for i in range(len(dates)):
            signal_i = self.signal_offset + i
            if carried_targets is not None and (i > 0 or signal_i > 0):
                was_flat = bool(carried_targets.get(FLAT))
                portfolio.cash = self._rebalance(portfolio, carried_targets, i, signal_i)
                # 每次撮合后同步策略真实持仓（换手缓冲带/持仓惯性策略需要）
                self.strategy.sync_positions(portfolio.positions)
                if was_flat and portfolio.positions:
                    # 清仓未完成时（跌停/停牌），保持 FLAT 意图隔日继续挂单，
                    # 且不调用 target_weights——策略在空仓态不应再产出选股
                    carried_targets = {FLAT: 1.0}
                    equity_values.append(portfolio.equity(md.close_val.iloc[i]))
                    continue
                carried_targets = None

            equity_values.append(portfolio.equity(md.close_val.iloc[i]))
            raw = self.strategy.target_weights(signal_i)
            if raw and FLAT in raw:
                carried_targets = {FLAT: 1.0}
            elif raw:
                carried_targets = dict(raw)
            else:
                carried_targets = None

        equity = pd.Series(equity_values, index=dates, name="equity")
        trades_df = pd.DataFrame([t.__dict__ for t in self.trades])
        return BacktestResult(
            equity=equity,
            trades=trades_df,
            strategy=getattr(self.strategy, "name", "unknown"),
            params=dict(getattr(self.strategy, "params", {})),
            initial_cash=float(starting_cash),
            ending_cash=float(portfolio.cash),
            ending_positions=dict(portfolio.positions),
            pending_targets=carried_targets,
            strategy_state=self.strategy.serialize_state(),
            rule_book_version=self.rule_resolver.version,
        )

    # ---------------- 内部实现 ----------------

    def _rebalance(
        self,
        portfolio: Portfolio,
        targets: dict[str, float],
        i: int,
        signal_i: int | None = None,
    ) -> float:
        """在 T+1 开盘执行 target weights，返回执行后的现金。"""
        md = self.md
        signal_i = self.signal_offset + i if signal_i is None else int(signal_i)
        prev_closes = self._previous_closes(i, signal_i)
        open_row = md.opens.iloc[i]
        adv = self._adv_row(i, signal_i)

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
            rule_resolver=self.rule_resolver,
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

    def _previous_closes(self, i: int, signal_i: int) -> pd.Series:
        """获取执行日前一收盘；测试段首日从信号上下文取历史收盘。"""
        if signal_i > 0:
            return self.signal_md.close_val.iloc[signal_i - 1]
        if i > 0:
            return self.md.close_val.iloc[i - 1]
        return self.md.close_val.iloc[0]

    def _adv_row(self, i: int, signal_i: int | None = None) -> pd.Series | None:
        """近 ADV_WINDOW 日平均成交额（不含当日，避免前视）。"""
        md = self.signal_md
        if md.amounts is None:
            return None
        signal_i = self.signal_offset + i if signal_i is None else int(signal_i)
        lo = max(0, signal_i - ADV_WINDOW)
        return md.amounts.iloc[lo:signal_i].mean()


__all__ = [
    "FLAT",
    "LOT",
    "MIN_ORDER_VALUE",
    "BacktestEngine",
    "BacktestResult",
    "BaseStrategy",
    "Fees",
    "MarketData",
    "Portfolio",
    "Trade",
    "limit_prices",
    "price_limit_pct",
]
