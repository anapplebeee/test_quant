"""手动交易 T+1 账户、计划、成交和对账 CLI。"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

from quart.config import PROJECT_ROOT
from quart.execution.fees import Fees
from quart.execution.models import BUY, SELL
from quart.manual_trading import FillInput, TradingRepository
from quart.manual_trading.io import import_fills_csv, load_snapshot_json, write_fill_template

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Quart 手动交易 T+1 同步")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "state" / "trading.db"))
    parser.add_argument("--account", default="manual")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="初始化账户")
    init.add_argument("--as-of", required=True)
    init.add_argument("--cash", type=float, default=None)
    init.add_argument("--holdings", default=str(PROJECT_ROOT / "state" / "holdings.json"))
    init.add_argument("--force", action="store_true")

    show = subparsers.add_parser("show", help="显示账户状态")
    show.add_argument("--as-of", default=date.today().isoformat())

    plans = subparsers.add_parser("plans", help="列出交易计划")
    plans.add_argument("--limit", type=int, default=20)

    plan = subparsers.add_parser("plan", help="显示计划详情")
    plan.add_argument("plan_id")

    approve = subparsers.add_parser("approve", help="审批计划")
    approve.add_argument("plan_id")

    cancel = subparsers.add_parser("cancel", help="取消计划")
    cancel.add_argument("plan_id")
    cancel.add_argument("--reason", default=None)

    fill = subparsers.add_parser("fill", help="录入单笔真实成交")
    fill.add_argument("--trade-date", required=True)
    fill.add_argument("--trade-time", default=None)
    fill.add_argument("--symbol", required=True)
    fill.add_argument("--side", choices=[BUY, SELL], required=True)
    fill.add_argument("--quantity", type=int, required=True)
    fill.add_argument("--price", type=float, required=True)
    fill.add_argument("--planned-order-id", type=int, default=None)
    fill.add_argument("--broker-fill-id", default=None)
    fill.add_argument("--commission", type=float, default=None)
    fill.add_argument("--stamp-tax", type=float, default=None)
    fill.add_argument("--transfer-fee", type=float, default=None)
    fill.add_argument("--other-fee", type=float, default=None)
    fill.add_argument("--settle-date", default=None)

    fills_import = subparsers.add_parser("fills-import", help="导入真实成交 CSV")
    fills_import.add_argument("file")
    fills_import.add_argument("--no-estimate-fees", action="store_true")

    template = subparsers.add_parser("fills-template", help="生成成交 CSV 模板")
    template.add_argument("file", nargs="?", default=str(PROJECT_ROOT / "state" / "fills_template.csv"))

    reconcile = subparsers.add_parser("reconcile", help="预览或确认券商账户快照对账")
    reconcile.add_argument("snapshot")
    reconcile.add_argument("--confirm", action="store_true")
    reconcile.add_argument("--resolution", default=None)

    args = parser.parse_args()
    repository = TradingRepository(args.db)
    repository.initialize_schema()
    if args.command == "init":
        _init(repository, args)
    elif args.command == "show":
        _show(repository, args.account, args.as_of)
    elif args.command == "plans":
        _plans(repository, args.limit)
    elif args.command == "plan":
        _plan(repository, args.plan_id)
    elif args.command == "approve":
        repository.approve_plan(args.plan_id)
        console.print(f"[green]计划已审批: {args.plan_id}[/green]")
    elif args.command == "cancel":
        repository.cancel_plan(args.plan_id, args.reason)
        console.print(f"[yellow]计划已取消: {args.plan_id}[/yellow]")
    elif args.command == "fill":
        _fill(repository, args)
    elif args.command == "fills-import":
        account_id = repository.get_or_create_account(args.account)
        fill_ids = import_fills_csv(
            repository,
            account_id,
            args.file,
            estimate_missing_fees=not args.no_estimate_fees,
        )
        console.print(f"[green]已导入 {len(fill_ids)} 笔成交: {fill_ids}[/green]")
    elif args.command == "fills-template":
        path = write_fill_template(args.file)
        console.print(f"[green]成交模板已生成: {path}[/green]")
    elif args.command == "reconcile":
        _reconcile(repository, args)


def _init(repository: TradingRepository, args) -> None:
    holdings_path = Path(args.holdings)
    if args.cash is None and holdings_path.exists():
        snapshot_id = repository.initialize_from_holdings_json(
            holdings_path,
            as_of=args.as_of,
            account_name=args.account,
            force=args.force,
        )
    else:
        if args.cash is None:
            raise SystemExit("未找到 holdings.json 时必须指定 --cash")
        snapshot_id = repository.initialize_account(
            cash=args.cash,
            positions={},
            as_of=args.as_of,
            account_name=args.account,
            force=args.force,
        )
    console.print(f"[green]账户已初始化, snapshot_id={snapshot_id}[/green]")


def _show(repository: TradingRepository, account_name: str, as_of: str) -> None:
    state = repository.account_state(account_name, as_of)
    if state is None:
        raise SystemExit("账户尚未初始化")
    console.print(
        f"账户 [bold]{state.account_name}[/bold] as_of={state.as_of} | "
        f"现金={state.cash_total:,.2f} 可交易={state.cash_available_to_trade:,.2f} "
        f"可取={state.cash_withdrawable:,.2f}"
    )
    table = Table("代码", "总持仓", "可卖", "成本价")
    for position in state.positions.values():
        table.add_row(
            position.symbol,
            str(position.total_quantity),
            str(position.sellable_quantity),
            f"{position.cost_price:.4f}",
        )
    console.print(table)


def _plans(repository: TradingRepository, limit: int) -> None:
    table = Table("plan_id", "账户", "策略", "信号日", "交易日", "状态", "订单数")
    for item in repository.list_plans(limit):
        table.add_row(
            item["plan_id"],
            item["account_name"],
            item["strategy_name"],
            item["signal_date"],
            item["intended_trade_date"],
            item["status"],
            str(item["order_count"]),
        )
    console.print(table)


def _plan(repository: TradingRepository, plan_id: str) -> None:
    detail = repository.plan_detail(plan_id)
    if detail is None:
        raise SystemExit(f"计划不存在: {plan_id}")
    console.print(json.dumps(detail["plan"], ensure_ascii=False, indent=2, default=str))
    table = Table("order_id", "方向", "代码", "策略数量", "批准数量", "已成交", "状态")
    for order in detail["orders"]:
        table.add_row(
            str(order["planned_order_id"]),
            order["side"],
            order["symbol"],
            str(order["strategy_quantity"]),
            str(order["approved_quantity"] or "-"),
            str(order["filled_quantity"]),
            order["status"],
        )
    console.print(table)


def _fill(repository: TradingRepository, args) -> None:
    state = repository.account_state(args.account, args.trade_date)
    if state is None:
        raise SystemExit("账户尚未初始化")
    estimated = all(
        value is None for value in (args.commission, args.stamp_tax, args.transfer_fee, args.other_fee)
    )
    commission = args.commission or 0.0
    stamp_tax = args.stamp_tax or 0.0
    transfer_fee = args.transfer_fee or 0.0
    other_fee = args.other_fee or 0.0
    source = "MANUAL"
    if estimated:
        amount = args.quantity * args.price
        fees = Fees.from_config()
        other_fee = fees.buy_cost(amount) if args.side == BUY else fees.sell_cost(amount)
        source = "MANUAL_ESTIMATED_FEES"
    fill_id = repository.record_fill(
        state.account_id,
        FillInput(
            symbol=args.symbol.zfill(6) if args.symbol.isdigit() else args.symbol,
            side=args.side,
            quantity=args.quantity,
            price=args.price,
            trade_date=args.trade_date,
            trade_time=args.trade_time,
            planned_order_id=args.planned_order_id,
            broker_fill_id=args.broker_fill_id,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            other_fee=other_fee,
            source=source,
            settle_date=args.settle_date,
        ),
    )
    console.print(f"[green]成交已记录: fill_id={fill_id}[/green]")


def _reconcile(repository: TradingRepository, args) -> None:
    payload = load_snapshot_json(args.snapshot)
    diff = repository.reconcile(
        account_name=args.account,
        as_of=payload["as_of"],
        cash_total=payload["cash_total"],
        cash_available=payload["cash_available_to_trade"],
        cash_withdrawable=payload["cash_withdrawable"],
        cash_frozen=payload["cash_frozen"],
        positions=payload["positions"],
        source=f"BROKER_JSON:{Path(args.snapshot).name}",
        confirm=args.confirm,
        resolution=args.resolution,
    )
    console.print(json.dumps(asdict(diff), ensure_ascii=False, indent=2))
    if not args.confirm:
        console.print("[yellow]当前仅预览差异; 确认无误后追加 --confirm[/yellow]")
    else:
        console.print(f"[green]对账已确认: reconciliation_id={diff.reconciliation_id}[/green]")


if __name__ == "__main__":
    main()
