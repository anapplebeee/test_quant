"""手动交易账户、计划、成交和对账数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from quart.domain import (
    SHANGHAI_TZ,
    OrderIntent,
    TradingEnvironment,
    market_datetime,
    new_id,
    stable_id,
    utc_now,
)
from quart.domain import (
    Fill as DomainFill,
)


@dataclass(frozen=True)
class PositionState:
    symbol: str
    total_quantity: int
    sellable_quantity: int
    frozen_quantity: int = 0
    cost_price: float = 0.0


@dataclass(frozen=True)
class AccountState:
    account_id: int
    account_name: str
    as_of: str
    cash_total: float
    cash_available_to_trade: float
    cash_withdrawable: float
    cash_frozen: float
    positions: dict[str, PositionState] = field(default_factory=dict)
    snapshot_id: int | None = None
    reconciliation_status: str | None = None

    @property
    def total_positions(self) -> dict[str, int]:
        return {symbol: position.total_quantity for symbol, position in self.positions.items()}

    @property
    def sellable_positions(self) -> dict[str, int]:
        return {symbol: position.sellable_quantity for symbol, position in self.positions.items()}


@dataclass(frozen=True)
class PlannedOrderInput:
    symbol: str
    side: str
    strategy_quantity: int
    reference_price: float
    target_weight: float = 0.0
    estimated_fee: float = 0.0
    deferred_quantity: int = 0

    def to_order_intent(
        self,
        *,
        account_id: int | str,
        planned_order_id: int | str | None = None,
        environment: TradingEnvironment | str = TradingEnvironment.PAPER,
        business_time: datetime | None = None,
        source: str = "MANUAL_TRADE_PLAN",
        reason: str = "",
        intent_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> OrderIntent:
        """把计划订单映射为风控前的规范委托意图。"""
        account_key = str(account_id)
        planned_key = str(planned_order_id) if planned_order_id is not None else None
        fallback_key = (
            f"planned:{account_key}:{planned_key}" if planned_key is not None else new_id("manual_intent_key")
        )
        final_key = idempotency_key or fallback_key
        return OrderIntent.create(
            account_id=account_key,
            environment=environment,
            symbol=self.symbol,
            side=self.side,
            quantity=self.strategy_quantity,
            business_time=business_time or utc_now(),
            source=source,
            reason=reason,
            limit_price=self.reference_price if self.reference_price > 0 else None,
            planned_order_id=planned_key,
            intent_id=intent_id or stable_id("intent", final_key),
            idempotency_key=final_key,
        )


@dataclass(frozen=True)
class FillInput:
    symbol: str
    side: str
    quantity: int
    price: float
    trade_date: str
    trade_time: str | None = None
    planned_order_id: int | None = None
    broker_fill_id: str | None = None
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    other_fee: float = 0.0
    source: str = "MANUAL"
    settle_date: str | None = None

    @property
    def amount(self) -> float:
        return self.quantity * self.price

    @property
    def total_fee(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee + self.other_fee

    def to_domain_fill(
        self,
        *,
        account_id: int | str,
        environment: TradingEnvironment | str = TradingEnvironment.PAPER,
        client_order_id: str | None = None,
        intent_id: str | None = None,
        event_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> DomainFill:
        """把人工录入或导入成交映射为可去重的领域成交。"""
        account_key = str(account_id)
        planned_key = str(self.planned_order_id) if self.planned_order_id is not None else None
        fill_key = idempotency_key or self.broker_fill_id or new_id("manual_fill")
        client_key = client_order_id or stable_id(
            "client_order",
            f"{account_key}:{planned_key or self.symbol}:{self.side}:{self.trade_date}",
        )
        return DomainFill.create(
            fill_id=stable_id("fill", f"{account_key}:{fill_key}"),
            event_id=event_id or stable_id("event", f"fill:{account_key}:{fill_key}"),
            client_order_id=client_key,
            intent_id=intent_id or stable_id("intent", client_key),
            account_id=account_key,
            environment=environment,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            business_time=market_datetime(self.trade_date, self.trade_time),
            source=self.source,
            idempotency_key=fill_key,
            broker_fill_id=self.broker_fill_id,
            planned_order_id=planned_key,
            commission=self.commission,
            stamp_tax=self.stamp_tax,
            transfer_fee=self.transfer_fee,
            other_fee=self.other_fee,
        )

    @classmethod
    def from_domain_fill(cls, fill: DomainFill) -> FillInput:
        """领域成交到现有 SQLite 账本输入的兼容转换。"""
        business_time = fill.business_time.astimezone(SHANGHAI_TZ)
        planned_order_id = int(fill.planned_order_id) if (fill.planned_order_id or "").isdigit() else None
        return cls(
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=float(fill.price),
            trade_date=business_time.date().isoformat(),
            trade_time=business_time.strftime("%H:%M:%S"),
            planned_order_id=planned_order_id,
            broker_fill_id=fill.broker_fill_id,
            commission=float(fill.commission),
            stamp_tax=float(fill.stamp_tax),
            transfer_fee=float(fill.transfer_fee),
            other_fee=float(fill.other_fee),
            source=fill.source,
        )


@dataclass(frozen=True)
class ReconciliationDiff:
    account_id: int
    as_of: str
    cash_total_difference: float
    cash_available_difference: float
    position_differences: dict[str, dict[str, int]]
    confirmed: bool = False
    reconciliation_id: int | None = None

    @property
    def matched(self) -> bool:
        return (
            abs(self.cash_total_difference) < 0.01
            and abs(self.cash_available_difference) < 0.01
            and not self.position_differences
        )
