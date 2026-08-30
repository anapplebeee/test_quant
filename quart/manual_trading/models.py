"""手动交易账户、计划、成交和对账数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


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
