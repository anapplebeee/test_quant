"""目标权重 → 委托计划的唯一实现（回测与实盘共用）。

这是整个执行层的核心不变量：**回测撮合与实盘下单必须走同一个函数**。
差异只允许来自注入的 `ExecutionModel`（成交价/可交易性）。

数值兼容说明
------------
使用 `BacktestExecutionModel` 且 `cash_buffer=0.995` 时，本函数的输出与
2026-08-28 通过终审的历史引擎逐笔一致（已由
`tests/test_execution_parity.py` 断言）。任何改动都必须先过该测试。
"""
from __future__ import annotations

import math

import pandas as pd

from quart.execution.constraints import FLAT
from quart.execution.models import (
    BUY,
    SELL,
    ExecutionContext,
    ExecutionModel,
    OrderPlan,
    RebalancePlan,
)


def _is_tradable(ctx: ExecutionContext, symbol: str) -> bool:
    if ctx.tradable is None:
        return True
    try:
        return bool(ctx.tradable.get(symbol, False))
    except (KeyError, TypeError):
        return False


def _price(ctx: ExecutionContext, series: pd.Series | None, symbol: str) -> float:
    if series is None:
        return float("nan")
    try:
        return float(series.get(symbol, float("nan")))
    except (KeyError, TypeError):
        return float("nan")


def _slip_notional(ctx: ExecutionContext, order_notional: float, position_notional: float) -> float:
    if ctx.slip_notional_mode == "order_value":
        return order_notional
    return position_notional


def generate_orders(ctx: ExecutionContext, model: ExecutionModel) -> RebalancePlan:
    """把目标权重转换为委托计划。纯函数，不修改入参。"""
    fees = ctx.fees
    lot = ctx.lot_size
    cash = float(ctx.cash)
    positions = {s: int(v) for s, v in ctx.positions.items() if int(v) > 0}

    is_flat = bool(ctx.targets.get(FLAT))
    targets = {s: float(w) for s, w in ctx.targets.items() if s != FLAT and float(w) > 0}

    orders: list[OrderPlan] = []
    skipped: list[OrderPlan] = []
    notes: list[str] = []

    equity_mark = float(ctx.equity)
    if not math.isfinite(equity_mark) or equity_mark <= 0:
        equity_mark = cash + sum(
            sh * _price(ctx, ctx.mark_prices, sym) for sym, sh in positions.items()
        )

    sell_proceeds = 0.0
    buy_notional = 0.0
    total_fee = 0.0

    # ---------------- 卖腿 ----------------
    # 先卖后买：A 股卖出资金当日可用于买入，且先卖能释放预算避免买不起。
    # 按代码排序保证同分结果确定（回测可复现）。
    for sym in sorted(positions.keys()):
        shares = positions[sym]
        sellable = (
            shares
            if ctx.sellable_positions is None
            else max(0, min(shares, int(ctx.sellable_positions.get(sym, 0))))
        )
        base_price = _price(ctx, ctx.exec_prices, sym)
        prev_close = _price(ctx, ctx.prev_closes, sym)
        ref_price = prev_close if math.isfinite(prev_close) else base_price

        if not math.isfinite(base_price) or base_price <= 0:
            skipped.append(OrderPlan(sym, SELL, 0, ref_price, blocked_reason="停牌/无行情"))
            continue
        if not _is_tradable(ctx, sym):
            skipped.append(OrderPlan(sym, SELL, 0, ref_price, blocked_reason="不可交易"))
            continue
        if reason := model.blocked_reason(sym, SELL, base_price, prev_close):
            skipped.append(OrderPlan(sym, SELL, shares, ref_price, base_price, blocked_reason=reason))
            continue

        # 不做 NaN→base_price 兜底：前收盘缺失时 position_notional 为 NaN，
        # 与历史引擎同口径（无法估值即不下单）。
        position_notional = shares * prev_close
        weight = 0.0

        if is_flat:
            requested_sell_shares = shares
        else:
            weight = float(targets.get(sym, 0.0))
            desired_value = weight * equity_mark
            delta = desired_value - position_notional
            if weight <= 0:
                requested_sell_shares = shares
            elif delta < -ctx.min_order_value:
                px0 = model.exec_price(sym, SELL, base_price, abs(delta), position_notional, _adv(ctx, sym))
                if not math.isfinite(px0) or px0 <= 0:
                    continue
                lots = math.floor(abs(delta) / (px0 * lot))
                requested_sell_shares = int(min(shares, lots * lot))
            else:
                continue

        sell_shares = min(requested_sell_shares, sellable)
        deferred_shares = requested_sell_shares - sell_shares
        if deferred_shares > 0:
            notes.append(
                f"{sym}: 计划卖出 {requested_sell_shares} 股，其中 {deferred_shares} 股受 T+1/冻结约束延期"
            )
        if sell_shares <= 0:
            if deferred_shares > 0:
                skipped.append(
                    OrderPlan(
                        sym,
                        SELL,
                        0,
                        ref_price,
                        base_price,
                        blocked_reason="T+1/冻结导致当日无可卖数量",
                        deferred_shares=deferred_shares,
                    )
                )
            continue
        # 清仓分支不检查 position_notional：一字板/停牌股也应尽力挂单卖出
        if not is_flat and not math.isfinite(position_notional):
            continue

        order_notional = sell_shares * base_price
        px = model.exec_price(
            sym, SELL, base_price, order_notional,
            _slip_notional(ctx, order_notional, position_notional), _adv(ctx, sym),
        )
        if not math.isfinite(px) or px <= 0:
            continue

        amount = sell_shares * px
        fee = fees.sell_cost(amount)
        cash += amount - fee
        sell_proceeds += amount
        total_fee += fee
        remaining = shares - sell_shares
        if remaining > 0:
            positions[sym] = remaining
        else:
            positions.pop(sym, None)
        orders.append(
            OrderPlan(
                symbol=sym,
                side=SELL,
                shares=sell_shares,
                ref_price=ref_price,
                exec_price=px,
                weight=weight,
                fee=fee,
                amount=amount,
                deferred_shares=deferred_shares,
            )
        )

    if is_flat:
        if positions:
            notes.append(
                f"{len(positions)} 只标的未能清仓（涨跌停/停牌），将在后续交易日继续挂单"
            )
        return RebalancePlan(
            orders=orders,
            skipped=skipped,
            ending_cash=cash,
            ending_positions=positions,
            sell_proceeds=sell_proceeds,
            buy_notional=0.0,
            total_fee=total_fee,
            notes=notes,
        )

    # ---------------- 买腿 ----------------
    # 按目标权重降序贪心分配：现金不足时优先满足高权重标的，
    # 保证"缺钱"降级为更小的组合而不是随机缺票。
    buy_syms = sorted(targets.items(), key=lambda kv: -kv[1])
    for sym, weight in buy_syms:
        base_price = _price(ctx, ctx.exec_prices, sym)
        prev_close = _price(ctx, ctx.prev_closes, sym)
        ref_price = prev_close if math.isfinite(prev_close) else base_price

        if not math.isfinite(base_price) or base_price <= 0:
            skipped.append(OrderPlan(sym, BUY, 0, ref_price, blocked_reason="停牌/无行情"))
            continue
        if not _is_tradable(ctx, sym):
            skipped.append(OrderPlan(sym, BUY, 0, ref_price, blocked_reason="不可交易"))
            continue
        if reason := model.blocked_reason(sym, BUY, base_price, prev_close):
            skipped.append(OrderPlan(sym, BUY, 0, ref_price, base_price, weight, blocked_reason=reason))
            continue

        shares_held = positions.get(sym, 0)
        # 与卖腿同口径：前收盘缺失时 delta 为 NaN，由下方 isfinite 校验拦截
        position_notional = shares_held * prev_close
        desired_value = weight * equity_mark
        delta = desired_value - position_notional

        budget = min(delta, cash * ctx.cash_buffer)
        if budget < max(ctx.min_order_value, lot * base_price):
            if delta >= max(ctx.min_order_value, lot * base_price):
                notes.append(f"{sym}: 可用资金不足，买入计划被裁剪")
            continue

        order_notional = min(budget, delta)
        px = model.exec_price(
            sym, BUY, base_price, order_notional,
            _slip_notional(ctx, order_notional, position_notional), _adv(ctx, sym),
        )
        if not math.isfinite(px) or px <= 0 or not math.isfinite(budget):
            continue
        if budget < max(ctx.min_order_value, lot * px):
            continue

        unit_cost = px
        if ctx.reserve_fees:
            unit_cost = px * (1 + fees.commission_rate + fees.transfer_fee_rate)
        shares_to_buy = math.floor(budget / (unit_cost * lot)) * lot
        shares_to_buy = int(shares_to_buy)
        if shares_to_buy < lot:
            if delta >= max(ctx.min_order_value, lot * px):
                notes.append(f"{sym}: 可用资金不足一手，买入计划被裁剪")
            continue

        amount = shares_to_buy * px
        fee = fees.buy_cost(amount)
        # 现金兜底：整手计算已预留费率，此处只对极端的 min_commission 边界做回退
        while shares_to_buy >= lot and amount + fee > cash:
            shares_to_buy -= lot
            amount = shares_to_buy * px
            fee = fees.buy_cost(amount)
        if shares_to_buy < lot:
            notes.append(f"{sym}: 可用资金不足一手，买入计划被裁剪")
            continue

        cash -= amount + fee
        positions[sym] = shares_held + shares_to_buy
        buy_notional += amount
        total_fee += fee
        orders.append(
            OrderPlan(
                symbol=sym,
                side=BUY,
                shares=shares_to_buy,
                ref_price=ref_price,
                exec_price=px,
                weight=weight,
                fee=fee,
                amount=amount,
            )
        )

    return RebalancePlan(
        orders=orders,
        skipped=skipped,
        ending_cash=cash,
        ending_positions=positions,
        sell_proceeds=sell_proceeds,
        buy_notional=buy_notional,
        total_fee=total_fee,
        notes=notes,
    )


def _adv(ctx: ExecutionContext, symbol: str) -> float:
    if ctx.adv is None:
        return 0.0
    try:
        v = float(ctx.adv.get(symbol, 0.0))
    except (KeyError, TypeError):
        return 0.0
    return v if math.isfinite(v) else 0.0


__all__ = ["generate_orders"]
