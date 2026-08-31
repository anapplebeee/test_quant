"""执行层数据模型：ExecutionContext / OrderPlan / RebalancePlan。

设计要点
--------
`generate_orders()` 是一个**纯函数**：给定 ExecutionContext，输出 RebalancePlan。
回测与实盘共用同一份实现，两者差异**只允许**通过 ExecutionModel 注入
（执行价、可交易性、拒单规则）。这消除"回测一套撮合、实盘另一套"的漂移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

from quart.domain import OrderIntent, TradingEnvironment, stable_id, utc_now
from quart.execution.constraints import A_SHARE_LOT
from quart.execution.fees import Fees

BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class ExecutionContext:
    """一次调仓的全部输入。

    Attributes
    ----------
    date:
        决策日（回测为 T 日收盘，实盘为最新交易日）。
    targets:
        目标权重 {symbol: weight}。已通过风控校验与归一化。
        `{FLAT: 1.0}` 表示清仓。
    equity:
        调仓前组合估值（用于把权重换算成目标金额）。
    cash:
        调仓前可用资金。
    positions:
        调仓前持仓 {symbol: shares}。
    sellable_positions:
        当日可卖持仓 {symbol: shares}。None 表示全部持仓均可卖（回测默认）；
        手动 A 股账户必须传入券商/账本可卖数量，防止 T 日买入仓位被列入
        T 日卖出计划。
    mark_prices:
        估值价（回测=前收盘，实盘=最新收盘）。
    exec_prices:
        执行价基准（回测=次日开盘，实盘=最新收盘参考价）。
    prev_closes:
        前收盘价，用于涨跌停判断。
    fees:
        费用与滑点模型。
    adv:
        近 N 日平均成交额，用于冲击成本；None 时跳过。
    tradable:
        可交易掩码（停牌/无行情=False）；None 表示不限制。
    lot_size:
        最小交易单位。
    min_order_value:
        低于此名义额不生成委托（避免碎股噪声）。
    cash_buffer:
        买入可用资金系数。回测保留 0.5% 现金垫，覆盖整手取整与费用的
        上界误差；实盘置 1.0。
    reserve_fees:
        整手计算时是否预留交易费用。预留后保证委托不会因费用而买不起。
    slip_notional_mode:
        "position_value" = 历史口径，冲击成本按持仓市值计（会高估小额减仓的冲击）
        "order_value"    = 修正口径，按本笔成交额计
    """

    date: pd.Timestamp
    targets: dict[str, float]
    equity: float
    cash: float
    positions: dict[str, int]
    mark_prices: pd.Series
    exec_prices: pd.Series
    prev_closes: pd.Series
    sellable_positions: dict[str, int] | None = None
    fees: Fees = field(default_factory=Fees)
    adv: pd.Series | None = None
    tradable: pd.Series | None = None
    lot_size: int = A_SHARE_LOT
    min_order_value: float = 1000.0
    cash_buffer: float = 1.0
    reserve_fees: bool = True
    slip_notional_mode: str = "position_value"


@dataclass(frozen=True)
class OrderPlan:
    """一笔委托计划。

    ref_price 是展示用参考价，exec_price 是撮合/估算用价格。回测中两者
    不同（参考前收盘、执行次日开盘）；实盘中两者相同。
    """

    symbol: str
    side: str
    shares: int
    ref_price: float
    exec_price: float = 0.0
    weight: float = 0.0
    fee: float = 0.0
    amount: float = 0.0
    blocked_reason: str | None = None
    deferred_shares: int = 0

    @property
    def action(self) -> str:
        """兼容旧字段名的别名。"""
        return self.side

    @property
    def notional(self) -> float:
        return self.amount or (self.shares * self.exec_price)

    def to_order_intent(
        self,
        *,
        account_id: int | str,
        environment: TradingEnvironment | str = TradingEnvironment.RESEARCH,
        business_time: datetime | None = None,
        source: str = "EXECUTION_PLAN",
        reason: str = "",
        planned_order_id: int | str | None = None,
        intent_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> OrderIntent:
        """把回测/信号共用的委托计划转换为风控前统一合同。"""
        account_key = str(account_id)
        planned_key = str(planned_order_id) if planned_order_id is not None else None
        event_time = business_time or utc_now()
        fallback_key = (
            f"execution-plan:{account_key}:{planned_key}"
            if planned_key is not None
            else stable_id(
                "execution_plan",
                f"{account_key}:{self.symbol}:{self.side}:{self.shares}:{event_time.isoformat()}",
            )
        )
        final_key = idempotency_key or fallback_key
        price = self.exec_price or self.ref_price
        return OrderIntent.create(
            account_id=account_key,
            environment=environment,
            symbol=self.symbol,
            side=self.side,
            quantity=self.shares,
            business_time=event_time,
            source=source,
            reason=reason or self.blocked_reason or "rebalance",
            limit_price=price if price > 0 else None,
            planned_order_id=planned_key,
            intent_id=intent_id or stable_id("intent", final_key),
            idempotency_key=final_key,
        )


@dataclass(frozen=True)
class RebalancePlan:
    """一次调仓的完整结果。"""

    orders: list[OrderPlan]
    skipped: list[OrderPlan]
    ending_cash: float
    ending_positions: dict[str, int]
    sell_proceeds: float = 0.0
    buy_notional: float = 0.0
    total_fee: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def filled(self) -> list[OrderPlan]:
        return self.orders


@runtime_checkable
class ExecutionModel(Protocol):
    """执行模型：决定"能不能成交"与"以什么价成交"。

    这是回测与实盘**唯一**允许存在差异的地方。差异必须显式、可审计，
    不能散落在撮合逻辑内部。
    """

    def exec_price(
        self,
        symbol: str,
        side: str,
        base_price: float,
        order_notional: float,
        position_notional: float,
        adv: float,
    ) -> float:
        """返回成交价。base_price 为执行价基准（回测=开盘，实盘=收盘）。"""
        ...

    def blocked_reason(
        self,
        symbol: str,
        side: str,
        base_price: float,
        prev_close: float,
    ) -> str | None:
        """返回拒单原因；None 表示可成交。"""
        ...


__all__ = [
    "BUY",
    "SELL",
    "ExecutionContext",
    "ExecutionModel",
    "OrderPlan",
    "RebalancePlan",
]
