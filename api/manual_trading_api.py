"""手动交易应用服务：为 Gradio 提供稳定、可测试的展示模型。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from common import load_stock_names
from quart.config import PROJECT_ROOT, load_config
from quart.data.store import BarStore
from quart.execution.fees import Fees
from quart.execution.models import BUY
from quart.manual_trading import FillInput, TradingRepository
from quart.manual_trading.io import export_plan_csv, import_fills_csv


def manual_settings() -> tuple[Path, str]:
    cfg = load_config().get("manual_trading", {})
    path = Path(cfg.get("database", PROJECT_ROOT / "state" / "trading.db"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path, str(cfg.get("account_name", "manual"))


def repository() -> TradingRepository:
    path, _ = manual_settings()
    repo = TradingRepository(path)
    repo.initialize_schema()
    return repo


def account_view(as_of: str | None = None) -> tuple[str, pd.DataFrame]:
    repo = repository()
    _, account_name = manual_settings()
    effective_date = _date_text(as_of or date.today().isoformat())
    state = repo.account_state(account_name, effective_date)
    if state is None:
        return "⚠️ 账户尚未初始化，请先录入券商收盘快照。", _empty_positions()

    names = load_stock_names()
    prices = _latest_prices(list(state.positions))
    rows = []
    market_value = 0.0
    unrealized = 0.0
    for position in state.positions.values():
        price = float(prices.get(position.symbol, 0.0))
        value = position.total_quantity * price
        pnl = (price - position.cost_price) * position.total_quantity if price > 0 else 0.0
        market_value += value
        unrealized += pnl
        rows.append(
            {
                "代码": position.symbol,
                "名称": names.get(position.symbol, "-"),
                "总持仓": position.total_quantity,
                "可卖": position.sellable_quantity,
                "T+1/冻结": position.total_quantity - position.sellable_quantity,
                "成本价": round(position.cost_price, 4),
                "最新价": round(price, 4) if price > 0 else None,
                "市值": round(value, 2),
                "浮动盈亏": round(pnl, 2),
            }
        )
    total = state.cash_total + market_value
    frame = pd.DataFrame(rows)
    if not frame.empty and total > 0:
        frame["权重%"] = (frame["市值"] / total * 100).round(2)
        frame = frame.sort_values("市值", ascending=False).reset_index(drop=True)
    else:
        frame = _empty_positions()
    summary = (
        f"**账户** `{state.account_name}`　**日期** `{effective_date}`　"
        f"**状态** `{state.reconciliation_status or '未对账'}`  \n"
        f"现金 **{state.cash_total:,.2f}**　可交易 **{state.cash_available_to_trade:,.2f}**　"
        f"持仓市值 **{market_value:,.2f}**　总资产 **{total:,.2f}**　"
        f"浮动盈亏 **{unrealized:+,.2f}**"
    )
    return summary, frame


def initialize_account_action(
    as_of: str,
    cash: float,
    positions_json: str,
    force: bool = False,
) -> tuple[str, str, pd.DataFrame]:
    try:
        positions = json.loads(positions_json or "{}")
        if not isinstance(positions, dict):
            raise ValueError("持仓 JSON 必须是对象")
        repo = repository()
        _, account_name = manual_settings()
        snapshot_id = repo.initialize_account(
            cash=float(cash),
            positions=positions,
            as_of=_date_text(as_of),
            account_name=account_name,
            source="FRONTEND_INIT",
            force=bool(force),
        )
        summary, frame = account_view(as_of)
        return f"✅ 账户快照已保存，snapshot_id={snapshot_id}", summary, frame
    except Exception as exc:
        summary, frame = account_view(as_of or None)
        return f"❌ 初始化失败：{exc}", summary, frame


def plans_view(limit: int = 50, as_of: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    repo = repository()
    _, account_name = manual_settings()
    repo.expire_plans(as_of or date.today().isoformat(), account_name)
    plans = repo.list_plans(limit=int(limit))
    rows = [
        {
            "计划ID": item["plan_id"],
            "策略": item["strategy_name"],
            "信号日": item["signal_date"],
            "交易日": item["intended_trade_date"],
            "状态": item["status"],
            "订单数": item["order_count"],
            "创建时间": item["created_at"],
        }
        for item in plans
    ]
    frame = pd.DataFrame(rows, columns=["计划ID", "策略", "信号日", "交易日", "状态", "订单数", "创建时间"])
    choices = [
        f"{item['plan_id']} | {item['intended_trade_date']} | {item['strategy_name']} | {item['status']}"
        for item in plans
    ]
    return frame, choices


def plan_view(choice: str | None) -> tuple[str, pd.DataFrame]:
    plan_id = plan_id_from_choice(choice)
    if not plan_id:
        return "请选择交易计划。", _empty_orders()
    detail = repository().plan_detail(plan_id)
    if detail is None:
        return f"⚠️ 计划不存在：`{plan_id}`", _empty_orders()
    plan = detail["plan"]
    frame = pd.DataFrame(detail["orders"])
    if frame.empty:
        frame = _empty_orders()
    else:
        rename = {
            "planned_order_id": "订单ID",
            "side": "方向",
            "symbol": "代码",
            "strategy_quantity": "策略数量",
            "approved_quantity": "批准数量",
            "filled_quantity": "已成交",
            "deferred_quantity": "延期数量",
            "reference_price": "参考价",
            "estimated_fee": "预计费用",
            "status": "状态",
            "adjustment_reason": "调整原因",
        }
        frame = frame.rename(columns=rename)
        frame = frame[[column for column in rename.values() if column in frame.columns]]
    message = (
        f"**计划** `{plan_id}`　**策略** `{plan['strategy_name']}`　**状态** `{plan['status']}`  \n"
        f"信号日 `{plan['signal_date']}` → 计划交易日 `{plan['intended_trade_date']}`　"
        f"来源制品 `{plan.get('source_run_id') or '-'}`"
    )
    return message, frame


def approve_plan_action(choice: str | None) -> str:
    try:
        plan_id = _required_plan_id(choice)
        repository().approve_plan(plan_id)
        return f"✅ 计划已审批：`{plan_id}`"
    except Exception as exc:
        return f"❌ 审批失败：{exc}"


def cancel_plan_action(choice: str | None, reason: str = "") -> str:
    try:
        plan_id = _required_plan_id(choice)
        repository().cancel_plan(plan_id, reason.strip() or None)
        return f"✅ 计划已取消：`{plan_id}`"
    except Exception as exc:
        return f"❌ 取消失败：{exc}"


def adjust_order_action(order_id: float | int, approved_quantity: float | int, reason: str) -> str:
    try:
        repository().adjust_planned_order(int(order_id), int(approved_quantity), reason.strip())
        return f"✅ 订单 {int(order_id)} 已调减为 {int(approved_quantity)} 股"
    except Exception as exc:
        return f"❌ 调整失败：{exc}"


def export_plan_action(choice: str | None) -> str | None:
    try:
        plan_id = _required_plan_id(choice)
        output = PROJECT_ROOT / "state" / "exports" / f"{plan_id}.csv"
        return str(export_plan_csv(repository(), plan_id, output))
    except Exception:
        return None


def record_fill_action(
    trade_date: str,
    trade_time: str,
    symbol: str,
    side: str,
    quantity: float | int,
    price: float,
    planned_order_id: float | int | None,
    broker_fill_id: str,
    commission: float,
    stamp_tax: float,
    transfer_fee: float,
    other_fee: float,
    settle_date: str,
    estimate_fees: bool = True,
) -> str:
    try:
        repo = repository()
        _, account_name = manual_settings()
        effective_date = _date_text(trade_date)
        state = repo.account_state(account_name, effective_date)
        if state is None:
            raise ValueError("账户尚未初始化")
        raw_symbol = str(symbol).strip()
        normalized_symbol = raw_symbol.zfill(6) if raw_symbol.isdigit() else raw_symbol
        normalized_side = str(side).upper()
        normalized_quantity = int(quantity)
        normalized_price = float(price)
        order_id = int(planned_order_id) if planned_order_id not in (None, "", 0) else None
        if order_id is None:
            order_id = repo.match_planned_order(
                state.account_id,
                normalized_symbol,
                normalized_side,
                effective_date,
                normalized_quantity,
            )
        fee_values = [float(value or 0.0) for value in (commission, stamp_tax, transfer_fee, other_fee)]
        source = "FRONTEND"
        if estimate_fees and not any(fee_values):
            amount = normalized_quantity * normalized_price
            fee_values[3] = (
                Fees.from_config().buy_cost(amount)
                if normalized_side == BUY
                else Fees.from_config().sell_cost(amount)
            )
            source = "FRONTEND_ESTIMATED_FEES"
        fill_id = repo.record_fill(
            state.account_id,
            FillInput(
                symbol=normalized_symbol,
                side=normalized_side,
                quantity=normalized_quantity,
                price=normalized_price,
                trade_date=effective_date,
                trade_time=trade_time.strip() or None,
                planned_order_id=order_id,
                broker_fill_id=broker_fill_id.strip() or None,
                commission=fee_values[0],
                stamp_tax=fee_values[1],
                transfer_fee=fee_values[2],
                other_fee=fee_values[3],
                source=source,
                settle_date=settle_date.strip() or None,
            ),
        )
        matched = f"，已匹配计划订单 {order_id}" if order_id else "，记录为计划外成交"
        return f"✅ 成交已记录：fill_id={fill_id}{matched}"
    except Exception as exc:
        return f"❌ 成交记录失败：{exc}"


def import_fills_action(file_path: str | None, estimate_fees: bool = True) -> str:
    if not file_path:
        return "❌ 请先上传成交 CSV"
    try:
        repo = repository()
        _, account_name = manual_settings()
        account_id = repo.get_or_create_account(account_name)
        fill_ids = import_fills_csv(repo, account_id, file_path, estimate_missing_fees=estimate_fees)
        return f"✅ 已导入 {len(fill_ids)} 笔成交：{fill_ids}"
    except Exception as exc:
        return f"❌ 导入失败：{exc}"


def fills_view(limit: int = 100) -> pd.DataFrame:
    repo = repository()
    _, account_name = manual_settings()
    rows = repo.list_fills(account_name, int(limit))
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["成交ID", "日期", "时间", "代码", "方向", "数量", "价格", "费用", "计划订单ID", "来源"])
    frame["费用"] = frame[["commission", "stamp_tax", "transfer_fee", "other_fee"]].sum(axis=1)
    frame = frame.rename(
        columns={
            "fill_id": "成交ID",
            "trade_date": "日期",
            "trade_time": "时间",
            "symbol": "代码",
            "side": "方向",
            "quantity": "数量",
            "price": "价格",
            "planned_order_id": "计划订单ID",
            "source": "来源",
        }
    )
    return frame[["成交ID", "日期", "时间", "代码", "方向", "数量", "价格", "费用", "计划订单ID", "来源"]]


def reconciliation_template(as_of: str | None = None) -> str:
    return json.dumps(
        {
            "as_of": _date_text(as_of or date.today().isoformat()),
            "cash_total": 1000000.0,
            "cash_available_to_trade": 1000000.0,
            "cash_withdrawable": 1000000.0,
            "cash_frozen": 0.0,
            "positions": {
                "600000": {
                    "total_quantity": 1000,
                    "sellable_quantity": 1000,
                    "cost_price": 10.0,
                }
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def reconcile_action(snapshot_json: str, confirm: bool = False, resolution: str = "") -> tuple[str, pd.DataFrame]:
    try:
        payload = json.loads(snapshot_json)
        required = {"as_of", "cash_total", "cash_available_to_trade", "positions"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"账户快照缺少字段: {missing}")
        repo = repository()
        _, account_name = manual_settings()
        diff = repo.reconcile(
            account_name=account_name,
            as_of=payload["as_of"],
            cash_total=float(payload["cash_total"]),
            cash_available=float(payload["cash_available_to_trade"]),
            cash_withdrawable=float(payload.get("cash_withdrawable", payload["cash_available_to_trade"])),
            cash_frozen=float(payload.get("cash_frozen", 0.0)),
            positions=payload["positions"],
            source="FRONTEND_BROKER_SNAPSHOT",
            confirm=bool(confirm),
            resolution=resolution.strip() or None,
        )
        rows = [
            {"代码": symbol, **values}
            for symbol, values in diff.position_differences.items()
        ]
        frame = pd.DataFrame(rows)
        state = "已确认并以券商快照覆盖账本" if diff.confirmed else "仅预览，账本未修改"
        message = (
            f"**{state}**  \n现金总额差异 `{diff.cash_total_difference:+,.2f}`，"
            f"可用资金差异 `{diff.cash_available_difference:+,.2f}`，"
            f"持仓差异 `{len(diff.position_differences)}` 项。"
        )
        return message, frame
    except Exception as exc:
        return f"❌ 对账失败：{exc}", pd.DataFrame()


def paper_trade_action(
    choice: str | None,
    execution_date: str | None = None,
    slippage_bps: float = 10.0,
    full_only: bool = False,
) -> str:
    """Paper Broker 模拟执行：把已审批计划的订单提交给模拟券商并回填账本。

    阶段 F 联调闭环（MANUAL_TRADING_T1_SYNC_PLAN.md）：
    计划 → BrokerOrderRequest → PaperBroker 状态机 → BrokerFill 回报 →
    `sync_broker_fills` 统一写入账本（与人工录入同一入账管线）。
    用于在接真实券商前验证"订单状态机 + 回报入账 + 计划状态推进"全链路。
    """
    try:
        plan_id = _required_plan_id(choice)
        repo = repository()
        detail = repo.plan_detail(plan_id)
        if detail is None:
            return f"❌ 计划不存在：`{plan_id}`"
        plan = detail["plan"]
        if plan["status"] != "APPROVED":
            return f"❌ 仅 APPROVED 计划可执行（当前 `{plan['status']}`）"

        from quart.broker.models import BrokerOrderRequest
        from quart.broker.paper import PaperBrokerAdapter
        from quart.broker.sync import sync_broker_fills

        account_id = plan["account_id"]
        trade_date = _date_text(execution_date or plan["intended_trade_date"])
        slip = float(slippage_bps) / 10_000.0
        adapter = PaperBrokerAdapter()
        broker_orders = []
        for order in detail["orders"]:
            quantity = int(order["approved_quantity"] or order["strategy_quantity"])
            if order["status"] in ("COMPLETED", "CANCELED", "EXPIRED") or quantity <= 0:
                continue
            request = BrokerOrderRequest(
                symbol=order["symbol"],
                side=order["side"],
                quantity=quantity,
                client_order_id=f"{plan_id}:{order['planned_order_id']}",
                planned_order_id=int(order["planned_order_id"]),
                account_id=str(account_id),
                environment="paper",
                reason=f"trade_plan:{plan_id}",
            )
            submitted = adapter.submit_order(request)
            # 模拟 T+1 成交：参考价 ± 不利方向滑点（与回测口径一致）
            ref = float(order["reference_price"] or 0.0)
            if ref <= 0:
                return f"❌ 订单 {order['planned_order_id']} 缺少参考价，无法模拟成交"
            exec_price = ref * (1 + slip) if order["side"] == BUY else ref * (1 - slip)
            adapter.apply_fill(
                submitted.broker_order_id,
                quantity,
                round(exec_price, 4),
                trade_date=trade_date,
                broker_fill_id=f"{plan_id}_f{order['planned_order_id']}",
            )
            broker_orders.append(submitted)

        fills = adapter.list_fills()
        if not fills:
            return "⚠️ 计划中没有可执行订单（可能已全部成交/取消）"

        fill_ids = sync_broker_fills(repo, account_id, fills, source="PAPER_BROKER")
        final_status = repo.plan_detail(plan_id)["plan"]["status"]
        total_amount = sum(f.quantity * f.price for f in fills)
        return (
            f"✅ Paper Broker 模拟执行完成：{len(fills)} 笔成交回填账本\n"
            f"fill_ids={fill_ids}\n"
            f"成交额 {total_amount:,.2f}（含估算费用），计划状态 → `{final_status}`\n"
            f"*这是联调用模拟成交，非真实下单；人工模式（记录成交/导入 CSV）不受影响。*"
        )
    except Exception as exc:
        return f"❌ Paper Broker 执行失败：{exc}"


def execution_view(choice: str | None) -> tuple[str, pd.DataFrame]:
    plan_id = plan_id_from_choice(choice)
    if not plan_id:
        return "请选择交易计划。", pd.DataFrame()
    rows = repository().execution_summary(plan_id)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return "该计划没有订单。", frame
    total_approved = int(frame["approved_quantity"].sum())
    total_filled = int(frame["filled_quantity"].sum())
    completion = total_filled / total_approved if total_approved else 1.0
    weighted_slippage = (
        (frame["slippage_bps"] * frame["filled_amount"]).sum() / frame["filled_amount"].sum()
        if frame["filled_amount"].sum() > 0
        else 0.0
    )
    message = (
        f"**计划** `{plan_id}`　完成率 **{completion:.1%}**　"
        f"成交额加权不利滑点 **{weighted_slippage:+.1f} bps**　"
        f"实际费用 **{frame['actual_fee'].sum():,.2f}**"
    )
    columns = {
        "planned_order_id": "订单ID",
        "symbol": "代码",
        "side": "方向",
        "approved_quantity": "批准数量",
        "filled_quantity": "成交数量",
        "remaining_quantity": "未成交",
        "reference_price": "参考价",
        "average_fill_price": "成交均价",
        "completion_pct": "完成率",
        "slippage_bps": "不利滑点bps",
        "estimated_fee": "预计费用",
        "actual_fee": "实际费用",
        "fee_difference": "费用偏差",
        "deferred_quantity": "延期数量",
    }
    frame = frame.rename(columns=columns)
    return message, frame[[column for column in columns.values() if column in frame.columns]]


def plan_id_from_choice(choice: str | None) -> str | None:
    text = str(choice or "").strip()
    if not text:
        return None
    plan_id = text.split("|", 1)[0].strip()
    return plan_id if plan_id.startswith("plan_") else None


def _required_plan_id(choice: str | None) -> str:
    plan_id = plan_id_from_choice(choice)
    if not plan_id:
        raise ValueError("请选择交易计划")
    return plan_id


def _date_text(value: str) -> str:
    return date.fromisoformat(str(value).strip()[:10]).isoformat()


def latest_prices(symbols: list[str]) -> dict[str, float]:
    """统一价格入口：走 BarStore 分区查询，兼容新旧布局。"""
    if not symbols:
        return {}
    try:
        bars = BarStore().load(symbols=symbols)
        if bars.empty:
            return {}
        latest = bars.sort_values("date").groupby("symbol", sort=False).tail(1)
        return dict(zip(latest["symbol"].astype(str), latest["close"].astype(float), strict=False))
    except Exception:
        return {}


def _latest_prices(symbols: list[str]) -> dict[str, float]:
    return latest_prices(symbols)


def _empty_positions() -> pd.DataFrame:
    return pd.DataFrame(columns=["代码", "名称", "总持仓", "可卖", "T+1/冻结", "成本价", "最新价", "市值", "浮动盈亏", "权重%"])


def _empty_orders() -> pd.DataFrame:
    return pd.DataFrame(columns=["订单ID", "方向", "代码", "策略数量", "批准数量", "已成交", "延期数量", "参考价", "状态"])


__all__ = [
    "account_view",
    "adjust_order_action",
    "approve_plan_action",
    "cancel_plan_action",
    "execution_view",
    "export_plan_action",
    "fills_view",
    "import_fills_action",
    "initialize_account_action",
    "latest_prices",
    "manual_settings",
    "paper_trade_action",
    "plan_id_from_choice",
    "plan_view",
    "plans_view",
    "reconcile_action",
    "reconciliation_template",
    "record_fill_action",
    "repository",
]
