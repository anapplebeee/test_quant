"""计划订单到 Paper 成交的执行偏差归因（M4）。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from quart.domain import BrokerOrder, OrderSide, OrderStatus
from quart.oms import OrderRepository


@dataclass(frozen=True)
class ExecutionAttributionSummary:
    """执行偏差汇总；无计划参考价时不伪造价格或机会成本。"""

    n_orders: int
    n_fully_filled_orders: int
    n_partially_filled_orders: int
    quantity_fill_rate: float
    n_price_observations: int
    mean_adverse_slippage_bps: float | None
    total_unfilled_reference_notional: float
    n_latency_observations: int
    median_first_fill_latency_seconds: float | None

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "n_orders": self.n_orders,
            "n_fully_filled_orders": self.n_fully_filled_orders,
            "n_partially_filled_orders": self.n_partially_filled_orders,
            "quantity_fill_rate": self.quantity_fill_rate,
            "n_price_observations": self.n_price_observations,
            "mean_adverse_slippage_bps": self.mean_adverse_slippage_bps,
            "total_unfilled_reference_notional": self.total_unfilled_reference_notional,
            "n_latency_observations": self.n_latency_observations,
            "median_first_fill_latency_seconds": self.median_first_fill_latency_seconds,
        }


def attribute_execution(
    orders: Iterable[BrokerOrder],
    *,
    reference_prices: Mapping[str, float] | None = None,
    first_fill_times: Mapping[str, datetime] | None = None,
) -> tuple[pd.DataFrame, ExecutionAttributionSummary]:
    """返回逐笔订单归因表和汇总。

    参考价按订单 ID 显式覆盖；否则只在订单具有正限价时使用限价。价格偏差统一
    为“对策略不利”的 bps（买入实际价更高、卖出实际价更低都为正）。未成交金额
    只是按参考价计量的机会暴露，不被误写成已实现损益。
    """
    overrides = reference_prices or {}
    fills_at = first_fill_times or {}
    rows: list[dict[str, object]] = []
    for order in sorted(orders, key=lambda item: (item.business_time, item.client_order_id)):
        reference = overrides.get(order.client_order_id, order.limit_price)
        reference_price = float(reference) if reference is not None and float(reference) > 0 else None
        average_fill_price = (
            float(order.average_fill_price) if order.filled_quantity > 0 else None
        )
        adverse_bps = None
        if reference_price is not None and average_fill_price is not None:
            adverse = (
                average_fill_price / reference_price - 1.0
                if order.side is OrderSide.BUY
                else reference_price / average_fill_price - 1.0
            )
            adverse_bps = adverse * 10_000
        first_fill_at = fills_at.get(order.client_order_id)
        latency_seconds = None
        if first_fill_at is not None:
            latency_seconds = (first_fill_at - order.business_time).total_seconds()
            if latency_seconds < 0:
                raise ValueError(f"成交时间早于委托时间: {order.client_order_id}")
        remaining = order.remaining_quantity
        rows.append({
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "status": order.status.value,
            "requested_quantity": order.requested_quantity,
            "approved_quantity": order.approved_quantity,
            "filled_quantity": order.filled_quantity,
            "remaining_quantity": remaining,
            "quantity_fill_rate": (
                order.filled_quantity / order.approved_quantity if order.approved_quantity else 0.0
            ),
            "reference_price": reference_price,
            "average_fill_price": average_fill_price,
            "adverse_slippage_bps": adverse_bps,
            "unfilled_reference_notional": (
                remaining * reference_price if reference_price is not None else None
            ),
            "first_fill_at": first_fill_at.isoformat() if first_fill_at is not None else None,
            "first_fill_latency_seconds": latency_seconds,
            "status_reason": order.status_reason,
        })
    columns = [
        "client_order_id", "symbol", "side", "status", "requested_quantity",
        "approved_quantity", "filled_quantity", "remaining_quantity", "quantity_fill_rate",
        "reference_price", "average_fill_price", "adverse_slippage_bps",
        "unfilled_reference_notional", "first_fill_at", "first_fill_latency_seconds", "status_reason",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    approved = int(frame["approved_quantity"].sum()) if not frame.empty else 0
    filled = int(frame["filled_quantity"].sum()) if not frame.empty else 0
    price_samples = frame["adverse_slippage_bps"].dropna()
    latency_samples = frame["first_fill_latency_seconds"].dropna()
    summary = ExecutionAttributionSummary(
        n_orders=len(frame),
        n_fully_filled_orders=int((frame["status"] == OrderStatus.FILLED.value).sum()),
        n_partially_filled_orders=int(
            (frame["status"] == OrderStatus.PARTIALLY_FILLED.value).sum()
        ),
        quantity_fill_rate=(filled / approved) if approved else 0.0,
        n_price_observations=len(price_samples),
        mean_adverse_slippage_bps=float(price_samples.mean()) if not price_samples.empty else None,
        total_unfilled_reference_notional=float(
            frame["unfilled_reference_notional"].fillna(0.0).sum()
        ),
        n_latency_observations=len(latency_samples),
        median_first_fill_latency_seconds=(
            float(latency_samples.median()) if not latency_samples.empty else None
        ),
    )
    return frame, summary


def attribute_paper_account(
    repository: OrderRepository,
    account_id: str,
    *,
    reference_prices: Mapping[str, float] | None = None,
) -> tuple[pd.DataFrame, ExecutionAttributionSummary]:
    """从 OMS 的订单与成交回报生成账户级执行归因。"""
    orders = repository.list_orders(account_id=account_id, limit=100_000)
    first_fill_times: dict[str, datetime] = {}
    for order in orders:
        reports = repository.list_reports(order.client_order_id)
        fill_times = [
            datetime.fromisoformat(str(report["business_time"]))
            for report in reports
            if int(report["last_filled_quantity"]) > 0
        ]
        if fill_times:
            first_fill_times[order.client_order_id] = min(fill_times)
    return attribute_execution(
        orders,
        reference_prices=reference_prices,
        first_fill_times=first_fill_times,
    )


__all__ = [
    "ExecutionAttributionSummary",
    "attribute_execution",
    "attribute_paper_account",
]
