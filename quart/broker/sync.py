"""券商回报 → 交易账本统一写入（FillService 桥接）。

规划（MANUAL_TRADING_T1_SYNC_PLAN.md 阶段 F）：人工导入与券商 Adapter 回报
必须走同一条入账通道。本模块把 `BrokerFill` 转写为 SQLite 账本的真实成交
（`source="BROKER_ADAPTER"`），复用账本的重复成交编号保护、T+1 可卖校验、
现金/持仓批次更新——与人工录入/CSV 导入完全一致。

未来接入真实券商时，只需在回报回调里调用 `sync_broker_fills`，无需改账本。
"""
from __future__ import annotations

from dataclasses import replace

from quart.broker.models import BrokerFill
from quart.manual_trading import FillInput, TradingRepository


def _to_fill_input(fill: BrokerFill, source: str) -> FillInput:
    return FillInput(
        symbol=fill.symbol,
        side=fill.side.upper(),
        quantity=fill.quantity,
        price=fill.price,
        trade_date=fill.trade_date,
        trade_time=fill.trade_time,
        planned_order_id=fill.planned_order_id,
        broker_fill_id=fill.broker_fill_id,
        commission=0.0,
        stamp_tax=0.0,
        transfer_fee=0.0,
        other_fee=0.0,
        source=source,
    )


def sync_broker_fills(
    repository: TradingRepository,
    account_id: int,
    fills: list[BrokerFill],
    source: str = "BROKER_ADAPTER",
    estimate_fees: bool = True,
) -> list[int]:
    """把券商成交回报批量写入账本（与人工导入同一 record_fill 管线）。

    - 交易费用：默认按配置估算（与 FRONTEND_ESTIMATED_FEES 同语义）；
    - `broker_fill_id` 重复时抛错，防重复入账（与人工导入一致）；
    - 返回新生成的 fill_id 列表。
    """
    if not fills:
        return []
    fill_ids: list[int] = []
    for fill in fills:
        payload = _to_fill_input(fill, source)
        if estimate_fees and not any(
            (payload.commission, payload.stamp_tax, payload.transfer_fee, payload.other_fee)
        ):
            from quart.execution.fees import Fees

            amount = fill.quantity * fill.price
            fees = Fees.from_config()
            fee = fees.buy_cost(amount) if fill.side.upper() == "BUY" else fees.sell_cost(amount)
            payload = replace(payload, other_fee=fee, source=f"{source}_ESTIMATED_FEES")
        fill_ids.append(repository.record_fill(account_id, payload))
    return fill_ids


__all__ = ["sync_broker_fills"]
