"""从 Paper 订单回报反向校准可执行成本（EXEC-002B3）。

本模块只汇总已落库的真实（或 Paper）订单及成交；没有足够样本时明确给出
``ready=False``，绝不以默认参数伪装成校准结论。建议的基础滑点取不利滑点
分布的保守分位数，作为后续人工审核后写入配置的候选值。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

from quart.domain import BrokerOrder, OrderSide, OrderStatus
from quart.oms import OrderRepository


@dataclass(frozen=True)
class PaperExecutionCalibration:
    """Paper 成交质量与成本校准结果；字段可直接写 JSON/Artifact。"""

    n_orders: int
    n_filled_orders: int
    n_partially_filled_orders: int
    quantity_fill_rate: float
    n_price_observations: int
    median_adverse_slippage: float | None
    conservative_adverse_slippage: float | None
    worst_adverse_slippage: float | None
    recommended_slippage_rate: float | None
    required_observations: int
    conservative_quantile: float

    @property
    def ready(self) -> bool:
        """只有足够价格样本才允许把建议值带入参数评审。"""
        return self.recommended_slippage_rate is not None

    def to_dict(self) -> dict[str, int | float | bool | None]:
        return {
            "n_orders": self.n_orders,
            "n_filled_orders": self.n_filled_orders,
            "n_partially_filled_orders": self.n_partially_filled_orders,
            "quantity_fill_rate": self.quantity_fill_rate,
            "n_price_observations": self.n_price_observations,
            "median_adverse_slippage": self.median_adverse_slippage,
            "conservative_adverse_slippage": self.conservative_adverse_slippage,
            "worst_adverse_slippage": self.worst_adverse_slippage,
            "recommended_slippage_rate": self.recommended_slippage_rate,
            "required_observations": self.required_observations,
            "conservative_quantile": self.conservative_quantile,
            "ready": self.ready,
        }


def calibrate_paper_execution(
    orders: Iterable[BrokerOrder],
    *,
    reference_prices: Mapping[str, float] | None = None,
    min_observations: int = 20,
    conservative_quantile: float = 0.75,
) -> PaperExecutionCalibration:
    """汇总订单成交率和买卖双向统一口径的不利滑点。

    ``reference_prices`` 按 ``client_order_id`` 覆盖订单限价；未传时可用的
    ``limit_price`` 被视为计划参考价。买入的 ``fill/reference - 1``、卖出的
    ``reference/fill - 1`` 都表示对策略不利的滑点，故可直接合并分位数。
    市价订单或无成交订单不参与价格校准，但仍进入成交率统计。
    """
    if min_observations <= 0:
        raise ValueError("min_observations 必须为正整数")
    if not 0 < conservative_quantile <= 1:
        raise ValueError("conservative_quantile 必须在 (0, 1] 区间")

    items = list(orders)
    approved = sum(order.approved_quantity for order in items)
    filled = sum(order.filled_quantity for order in items)
    price_rows: list[float] = []
    overrides = reference_prices or {}
    for order in items:
        reference = overrides.get(order.client_order_id, order.limit_price)
        if (
            reference is None
            or float(reference) <= 0
            or order.filled_quantity <= 0
            or float(order.average_fill_price) <= 0
        ):
            continue
        ref = float(reference)
        fill = float(order.average_fill_price)
        adverse_slippage = fill / ref - 1.0 if order.side is OrderSide.BUY else ref / fill - 1.0
        price_rows.append(adverse_slippage)

    slippage = pd.Series(price_rows, dtype=float)
    n_observations = len(slippage)
    conservative = (
        float(slippage.quantile(conservative_quantile)) if n_observations else None
    )
    recommendation = (
        max(0.0, conservative)
        if conservative is not None and n_observations >= min_observations
        else None
    )
    return PaperExecutionCalibration(
        n_orders=len(items),
        n_filled_orders=sum(order.status is OrderStatus.FILLED for order in items),
        n_partially_filled_orders=sum(
            order.status is OrderStatus.PARTIALLY_FILLED for order in items
        ),
        quantity_fill_rate=(filled / approved) if approved else 0.0,
        n_price_observations=n_observations,
        median_adverse_slippage=float(slippage.median()) if n_observations else None,
        conservative_adverse_slippage=conservative,
        worst_adverse_slippage=float(slippage.max()) if n_observations else None,
        recommended_slippage_rate=recommendation,
        required_observations=int(min_observations),
        conservative_quantile=float(conservative_quantile),
    )


def calibrate_paper_account(
    repository: OrderRepository,
    account_id: str,
    *,
    reference_prices: Mapping[str, float] | None = None,
    min_observations: int = 20,
    conservative_quantile: float = 0.75,
) -> PaperExecutionCalibration:
    """从 OMS 读取一个账户的全量订单，生成确定性的校准报告。"""
    return calibrate_paper_execution(
        repository.list_orders(account_id=account_id, limit=100_000),
        reference_prices=reference_prices,
        min_observations=min_observations,
        conservative_quantile=conservative_quantile,
    )


__all__ = [
    "PaperExecutionCalibration",
    "calibrate_paper_account",
    "calibrate_paper_execution",
]
