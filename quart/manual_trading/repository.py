"""SQLite 手动交易账本。

计划不会改变账户; 只有初始/对账快照与真实成交可以改变现金和持仓。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from quart.config import PROJECT_ROOT
from quart.execution.models import BUY, SELL
from quart.manual_trading.models import (
    AccountState,
    FillInput,
    PlannedOrderInput,
    PositionState,
    ReconciliationDiff,
)

DEFAULT_ACCOUNT_NAME = "manual"
DEFAULT_DB_PATH = PROJECT_ROOT / "state" / "trading.db"


def _iso_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def next_trade_date(value: str | date | datetime, trading_dates: Sequence[str] | None = None) -> str:
    """返回下一交易日。

    有交易日序列时使用序列; 否则只跳过周末。节假日场景应由 CLI 显式传入
    `settle_date`, 或在后续规则引擎接入权威交易日历。
    """
    current = date.fromisoformat(_iso_date(value))
    if trading_dates:
        candidates = sorted(date.fromisoformat(_iso_date(item)) for item in trading_dates)
        future = next((item for item in candidates if item > current), None)
        if future is not None:
            return future.isoformat()
    candidate = current + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


class TradingRepository:
    """单机手动交易账本与计划仓库。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def get_or_create_account(self, name: str = DEFAULT_ACCOUNT_NAME, broker_name: str = "manual") -> int:
        self.initialize_schema()
        with self._connect() as connection:
            row = connection.execute("SELECT account_id FROM accounts WHERE account_name = ?", (name,)).fetchone()
            if row is not None:
                return int(row["account_id"])
            cursor = connection.execute(
                "INSERT INTO accounts(account_name, broker_name, status, created_at) VALUES (?, ?, 'ACTIVE', ?)",
                (name, broker_name, _now()),
            )
            return int(cursor.lastrowid)

    def initialize_account(
        self,
        cash: float,
        positions: dict[str, int | dict],
        as_of: str | date | datetime,
        account_name: str = DEFAULT_ACCOUNT_NAME,
        source: str = "MANUAL_INIT",
        force: bool = False,
    ) -> int:
        account_id = self.get_or_create_account(account_name)
        normalized = _normalize_positions(positions)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM account_snapshots WHERE account_id = ? LIMIT 1", (account_id,)
            ).fetchone()
            if exists is not None and not force:
                raise ValueError(f"账户 {account_name!r} 已初始化; 如需覆盖请显式使用 force")
            snapshot_id = self._write_snapshot_and_replace(
                connection,
                account_id=account_id,
                as_of=_iso_date(as_of),
                cash_total=float(cash),
                cash_available=float(cash),
                cash_withdrawable=float(cash),
                cash_frozen=0.0,
                positions=normalized,
                source=source,
                reconciliation_status="RECONCILED",
            )
            return snapshot_id

    def initialize_from_holdings_json(
        self,
        path: Path | str,
        as_of: str | date | datetime,
        account_name: str = DEFAULT_ACCOUNT_NAME,
        force: bool = False,
    ) -> int:
        source_path = Path(path)
        with source_path.open(encoding="utf-8") as file:
            payload = json.load(file)
        return self.initialize_account(
            cash=float(payload.get("cash", 0.0)),
            positions=payload.get("positions", {}),
            as_of=as_of,
            account_name=account_name,
            source=f"LEGACY_JSON:{source_path.name}",
            force=force,
        )

    def has_snapshot(self, account_name: str = DEFAULT_ACCOUNT_NAME) -> bool:
        if not self.path.exists():
            return False
        self.initialize_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM account_snapshots s
                JOIN accounts a ON a.account_id = s.account_id
                WHERE a.account_name = ?
                LIMIT 1
                """,
                (account_name,),
            ).fetchone()
            return row is not None

    def account_state(
        self,
        account_name: str = DEFAULT_ACCOUNT_NAME,
        as_of: str | date | datetime | None = None,
    ) -> AccountState | None:
        if not self.path.exists():
            return None
        self.initialize_schema()
        effective_date = _iso_date(as_of or date.today())
        with self._connect() as connection:
            account = connection.execute(
                "SELECT account_id, account_name FROM accounts WHERE account_name = ?", (account_name,)
            ).fetchone()
            if account is None:
                return None
            account_id = int(account["account_id"])
            balance = connection.execute(
                "SELECT * FROM account_balances WHERE account_id = ?", (account_id,)
            ).fetchone()
            if balance is None:
                return None
            position_rows = connection.execute(
                """
                SELECT symbol,
                       SUM(remaining_quantity) AS total_quantity,
                       SUM(CASE WHEN settle_date <= ? THEN remaining_quantity ELSE 0 END) AS sellable_quantity,
                       CASE WHEN SUM(remaining_quantity) > 0
                            THEN SUM(remaining_quantity * unit_cost) / SUM(remaining_quantity)
                            ELSE 0 END AS cost_price
                FROM position_lots
                WHERE account_id = ? AND remaining_quantity > 0
                GROUP BY symbol
                ORDER BY symbol
                """,
                (effective_date, account_id),
            ).fetchall()
            positions = {
                str(row["symbol"]): PositionState(
                    symbol=str(row["symbol"]),
                    total_quantity=int(row["total_quantity"] or 0),
                    sellable_quantity=int(row["sellable_quantity"] or 0),
                    cost_price=float(row["cost_price"] or 0.0),
                )
                for row in position_rows
            }
            snapshot = connection.execute(
                """
                SELECT snapshot_id, reconciliation_status
                FROM account_snapshots
                WHERE account_id = ? AND as_of <= ? AND reconciliation_status = 'RECONCILED'
                ORDER BY as_of DESC, snapshot_id DESC
                LIMIT 1
                """,
                (account_id, effective_date),
            ).fetchone()
            return AccountState(
                account_id=account_id,
                account_name=str(account["account_name"]),
                as_of=effective_date,
                cash_total=float(balance["cash_total"]),
                cash_available_to_trade=float(balance["cash_available_to_trade"]),
                cash_withdrawable=float(balance["cash_withdrawable"]),
                cash_frozen=float(balance["cash_frozen"]),
                positions=positions,
                snapshot_id=int(snapshot["snapshot_id"]) if snapshot is not None else None,
                reconciliation_status=(
                    str(snapshot["reconciliation_status"]) if snapshot is not None else None
                ),
            )

    def create_trade_plan(
        self,
        account_id: int,
        strategy_name: str,
        signal_date: str | date | datetime,
        intended_trade_date: str | date | datetime,
        orders: Sequence[PlannedOrderInput],
        source_run_id: str | None = None,
        config_fingerprint: str | None = None,
        account_snapshot_id: int | None = None,
        notes: str | None = None,
    ) -> str:
        self.initialize_schema()
        signal = _iso_date(signal_date)
        intended = _iso_date(intended_trade_date)
        if intended <= signal:
            raise ValueError("手动交易计划必须在信号日之后执行 (T+1 或更晚)")
        plan_id = f"plan_{signal.replace('-', '')}_{uuid.uuid4().hex[:8]}"
        with self._connect() as connection:
            approved = connection.execute(
                """
                SELECT plan_id FROM trade_plans
                WHERE account_id = ? AND strategy_name = ? AND signal_date = ?
                  AND intended_trade_date = ? AND status IN ('APPROVED', 'IN_PROGRESS', 'PARTIAL')
                LIMIT 1
                """,
                (account_id, strategy_name, signal, intended),
            ).fetchone()
            if approved is not None:
                raise ValueError(f"同日已有已审批或执行中的计划: {approved['plan_id']}")
            connection.execute(
                """
                UPDATE trade_plans SET status = 'SUPERSEDED', completed_at = ?
                WHERE account_id = ? AND strategy_name = ? AND signal_date = ?
                  AND intended_trade_date = ? AND status = 'DRAFT'
                """,
                (_now(), account_id, strategy_name, signal, intended),
            )
            connection.execute(
                """
                INSERT INTO trade_plans(
                    plan_id, account_id, account_snapshot_id, strategy_name,
                    signal_date, intended_trade_date, status, source_run_id,
                    config_fingerprint, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    account_id,
                    account_snapshot_id,
                    strategy_name,
                    signal,
                    intended,
                    source_run_id,
                    config_fingerprint,
                    _now(),
                    notes,
                ),
            )
            for order in orders:
                side = str(order.side).upper()
                if side not in (BUY, SELL):
                    raise ValueError(f"未知买卖方向: {order.side}")
                if int(order.strategy_quantity) <= 0:
                    continue
                connection.execute(
                    """
                    INSERT INTO planned_orders(
                        plan_id, symbol, side, strategy_quantity, approved_quantity,
                        reference_price, target_weight, estimated_fee,
                        deferred_quantity, status
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, 'DRAFT')
                    """,
                    (
                        plan_id,
                        str(order.symbol),
                        side,
                        int(order.strategy_quantity),
                        float(order.reference_price),
                        float(order.target_weight),
                        float(order.estimated_fee),
                        int(order.deferred_quantity),
                    ),
                )
        return plan_id

    def attach_source_run(self, plan_id: str, source_run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE trade_plans SET source_run_id = ? WHERE plan_id = ?",
                (source_run_id, plan_id),
            )

    def approve_plan(self, plan_id: str) -> None:
        with self._connect() as connection:
            plan = connection.execute(
                "SELECT status FROM trade_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise KeyError(f"交易计划不存在: {plan_id}")
            if plan["status"] != "DRAFT":
                raise ValueError(f"只有 DRAFT 计划可以审批, 当前状态: {plan['status']}")
            connection.execute(
                """
                UPDATE planned_orders
                SET approved_quantity = COALESCE(approved_quantity, strategy_quantity), status = 'APPROVED'
                WHERE plan_id = ?
                """,
                (plan_id,),
            )
            connection.execute(
                "UPDATE trade_plans SET status = 'APPROVED', approved_at = ? WHERE plan_id = ?",
                (_now(), plan_id),
            )

    def adjust_planned_order(self, planned_order_id: int, approved_quantity: int, reason: str) -> None:
        if approved_quantity < 0:
            raise ValueError("批准数量不能为负")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT o.strategy_quantity, p.status
                FROM planned_orders o JOIN trade_plans p ON p.plan_id = o.plan_id
                WHERE o.planned_order_id = ?
                """,
                (planned_order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"计划订单不存在: {planned_order_id}")
            if row["status"] != "DRAFT":
                raise ValueError("只能调整 DRAFT 计划")
            if approved_quantity > int(row["strategy_quantity"]):
                raise ValueError("手动模式只允许调减计划数量; 新增交易请作为计划外交易记录")
            connection.execute(
                """
                UPDATE planned_orders
                SET approved_quantity = ?, adjustment_reason = ?
                WHERE planned_order_id = ?
                """,
                (approved_quantity, reason, planned_order_id),
            )

    def cancel_plan(self, plan_id: str, reason: str | None = None) -> None:
        with self._connect() as connection:
            plan = connection.execute(
                "SELECT status FROM trade_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                raise KeyError(f"交易计划不存在: {plan_id}")
            if plan["status"] in ("COMPLETED", "CANCELED", "SUPERSEDED"):
                raise ValueError(f"计划当前状态不可取消: {plan['status']}")
            connection.execute(
                "UPDATE trade_plans SET status = 'CANCELED', completed_at = ?, notes = COALESCE(?, notes) WHERE plan_id = ?",
                (_now(), reason, plan_id),
            )
            connection.execute(
                "UPDATE planned_orders SET status = 'CANCELED' WHERE plan_id = ? AND status != 'COMPLETED'",
                (plan_id,),
            )

    def list_plans(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        self.initialize_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, a.account_name,
                       COUNT(o.planned_order_id) AS order_count
                FROM trade_plans p
                JOIN accounts a ON a.account_id = p.account_id
                LEFT JOIN planned_orders o ON o.plan_id = p.plan_id
                GROUP BY p.plan_id
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def plan_detail(self, plan_id: str) -> dict | None:
        if not self.path.exists():
            return None
        self.initialize_schema()
        with self._connect() as connection:
            plan = connection.execute(
                "SELECT * FROM trade_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan is None:
                return None
            orders = connection.execute(
                """
                SELECT o.*,
                       COALESCE(SUM(f.quantity), 0) AS filled_quantity
                FROM planned_orders o
                LEFT JOIN manual_fills f ON f.planned_order_id = o.planned_order_id
                WHERE o.plan_id = ?
                GROUP BY o.planned_order_id
                ORDER BY CASE o.side WHEN 'SELL' THEN 0 ELSE 1 END, o.symbol
                """,
                (plan_id,),
            ).fetchall()
            return {"plan": dict(plan), "orders": [dict(row) for row in orders]}

    def record_fill(self, account_id: int, fill: FillInput) -> int:
        side = fill.side.upper()
        if side not in (BUY, SELL):
            raise ValueError(f"未知买卖方向: {fill.side}")
        if fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("成交数量和价格必须为正")
        trade_date = _iso_date(fill.trade_date)
        with self._connect() as connection:
            if fill.broker_fill_id:
                duplicate = connection.execute(
                    "SELECT fill_id FROM manual_fills WHERE account_id = ? AND broker_fill_id = ?",
                    (account_id, fill.broker_fill_id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(f"成交编号重复: {fill.broker_fill_id}")

            if fill.planned_order_id is not None:
                planned = connection.execute(
                    """
                    SELECT o.*, p.status AS plan_status
                    FROM planned_orders o JOIN trade_plans p ON p.plan_id = o.plan_id
                    WHERE o.planned_order_id = ? AND p.account_id = ?
                    """,
                    (fill.planned_order_id, account_id),
                ).fetchone()
                if planned is None:
                    raise KeyError(f"计划订单不存在: {fill.planned_order_id}")
                if planned["plan_status"] not in ("APPROVED", "IN_PROGRESS", "PARTIAL"):
                    raise ValueError(f"计划尚未审批或不可执行: {planned['plan_status']}")
                if planned["symbol"] != fill.symbol or planned["side"] != side:
                    raise ValueError("成交代码或方向与计划订单不一致")
                approved_quantity = int(planned["approved_quantity"] or 0)
                filled_quantity = int(connection.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM manual_fills WHERE planned_order_id = ?",
                    (fill.planned_order_id,),
                ).fetchone()[0])
                if filled_quantity + fill.quantity > approved_quantity:
                    raise ValueError("成交数量超过已审批的剩余数量")

            balance = connection.execute(
                "SELECT * FROM account_balances WHERE account_id = ?", (account_id,)
            ).fetchone()
            if balance is None:
                raise ValueError("账户尚未初始化或对账")

            amount = float(fill.amount)
            total_fee = float(fill.total_fee)
            cash_total = float(balance["cash_total"])
            cash_available = float(balance["cash_available_to_trade"])
            cash_withdrawable = float(balance["cash_withdrawable"])
            cash_frozen = float(balance["cash_frozen"])

            if side == BUY:
                total_cost = amount + total_fee
                if total_cost > cash_available + 0.01:
                    raise ValueError(f"可用资金不足: 需要 {total_cost:.2f}, 当前 {cash_available:.2f}")
                cash_total -= total_cost
                cash_available -= total_cost
                cash_withdrawable = max(0.0, cash_withdrawable - total_cost)
            else:
                self._consume_sellable_lots(connection, account_id, fill.symbol, fill.quantity, trade_date)
                net = amount - total_fee
                cash_total += net
                cash_available += net

            cursor = connection.execute(
                """
                INSERT INTO manual_fills(
                    account_id, planned_order_id, broker_fill_id, trade_date, trade_time,
                    symbol, side, quantity, price, amount, commission, stamp_tax,
                    transfer_fee, other_fee, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    fill.planned_order_id,
                    fill.broker_fill_id,
                    trade_date,
                    fill.trade_time,
                    fill.symbol,
                    side,
                    fill.quantity,
                    fill.price,
                    amount,
                    fill.commission,
                    fill.stamp_tax,
                    fill.transfer_fee,
                    fill.other_fee,
                    fill.source,
                    _now(),
                ),
            )
            fill_id = int(cursor.lastrowid)
            if side == BUY:
                settle_date = _iso_date(fill.settle_date) if fill.settle_date else next_trade_date(trade_date)
                unit_cost = (amount + total_fee) / fill.quantity
                connection.execute(
                    """
                    INSERT INTO position_lots(
                        account_id, symbol, buy_trade_date, settle_date,
                        original_quantity, remaining_quantity, unit_cost,
                        source_fill_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (
                        account_id,
                        fill.symbol,
                        trade_date,
                        settle_date,
                        fill.quantity,
                        fill.quantity,
                        unit_cost,
                        fill_id,
                    ),
                )
            connection.execute(
                """
                UPDATE account_balances
                SET cash_total = ?, cash_available_to_trade = ?, cash_withdrawable = ?,
                    cash_frozen = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (cash_total, cash_available, cash_withdrawable, cash_frozen, _now(), account_id),
            )
            if fill.planned_order_id is not None:
                self._refresh_plan_status(connection, fill.planned_order_id)
            return fill_id

    def reconcile(
        self,
        account_name: str,
        as_of: str | date | datetime,
        cash_total: float,
        cash_available: float,
        cash_withdrawable: float,
        positions: dict[str, int | dict],
        cash_frozen: float = 0.0,
        source: str = "BROKER_SNAPSHOT",
        confirm: bool = False,
        resolution: str | None = None,
    ) -> ReconciliationDiff:
        account_id = self.get_or_create_account(account_name)
        effective_date = _iso_date(as_of)
        current = self.account_state(account_name, effective_date)
        normalized = _normalize_positions(positions)
        current_positions = current.positions if current else {}
        symbols = sorted(set(current_positions) | set(normalized))
        position_differences: dict[str, dict[str, int]] = {}
        for symbol in symbols:
            ledger = current_positions.get(symbol)
            broker = normalized.get(symbol)
            ledger_total = ledger.total_quantity if ledger else 0
            ledger_sellable = ledger.sellable_quantity if ledger else 0
            broker_total = int(broker["total_quantity"]) if broker else 0
            broker_sellable = int(broker["sellable_quantity"]) if broker else 0
            if ledger_total != broker_total or ledger_sellable != broker_sellable:
                position_differences[symbol] = {
                    "ledger_total": ledger_total,
                    "broker_total": broker_total,
                    "ledger_sellable": ledger_sellable,
                    "broker_sellable": broker_sellable,
                }
        ledger_cash_total = current.cash_total if current else 0.0
        ledger_cash_available = current.cash_available_to_trade if current else 0.0
        cash_total_difference = float(cash_total) - ledger_cash_total
        cash_available_difference = float(cash_available) - ledger_cash_available
        reconciliation_id = None
        if confirm:
            with self._connect() as connection:
                snapshot_id = self._write_snapshot_and_replace(
                    connection,
                    account_id=account_id,
                    as_of=effective_date,
                    cash_total=float(cash_total),
                    cash_available=float(cash_available),
                    cash_withdrawable=float(cash_withdrawable),
                    cash_frozen=float(cash_frozen),
                    positions=normalized,
                    source=source,
                    reconciliation_status="RECONCILED",
                )
                cursor = connection.execute(
                    """
                    INSERT INTO reconciliations(
                        account_id, as_of, broker_snapshot_id, status,
                        cash_total_difference, cash_available_difference,
                        position_difference_count, details_json, resolution,
                        confirmed_at
                    ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        effective_date,
                        snapshot_id,
                        cash_total_difference,
                        cash_available_difference,
                        len(position_differences),
                        json.dumps(position_differences, ensure_ascii=False, sort_keys=True),
                        resolution,
                        _now(),
                    ),
                )
                reconciliation_id = int(cursor.lastrowid)
        return ReconciliationDiff(
            account_id=account_id,
            as_of=effective_date,
            cash_total_difference=cash_total_difference,
            cash_available_difference=cash_available_difference,
            position_differences=position_differences,
            confirmed=confirm,
            reconciliation_id=reconciliation_id,
        )

    def _consume_sellable_lots(
        self,
        connection: sqlite3.Connection,
        account_id: int,
        symbol: str,
        quantity: int,
        trade_date: str,
    ) -> None:
        lots = connection.execute(
            """
            SELECT lot_id, remaining_quantity
            FROM position_lots
            WHERE account_id = ? AND symbol = ? AND remaining_quantity > 0 AND settle_date <= ?
            ORDER BY settle_date, buy_trade_date, lot_id
            """,
            (account_id, symbol, trade_date),
        ).fetchall()
        available = sum(int(row["remaining_quantity"]) for row in lots)
        if quantity > available:
            raise ValueError(f"{symbol} 可卖数量不足: 需要 {quantity}, 当前 {available}")
        remaining = quantity
        for lot in lots:
            if remaining <= 0:
                break
            lot_quantity = int(lot["remaining_quantity"])
            consumed = min(remaining, lot_quantity)
            new_quantity = lot_quantity - consumed
            connection.execute(
                "UPDATE position_lots SET remaining_quantity = ?, status = ? WHERE lot_id = ?",
                (new_quantity, "CLOSED" if new_quantity == 0 else "OPEN", int(lot["lot_id"])),
            )
            remaining -= consumed

    def _refresh_plan_status(self, connection: sqlite3.Connection, planned_order_id: int) -> None:
        row = connection.execute(
            "SELECT plan_id FROM planned_orders WHERE planned_order_id = ?", (planned_order_id,)
        ).fetchone()
        if row is None:
            return
        plan_id = str(row["plan_id"])
        orders = connection.execute(
            """
            SELECT o.planned_order_id, o.approved_quantity,
                   COALESCE(SUM(f.quantity), 0) AS filled_quantity
            FROM planned_orders o
            LEFT JOIN manual_fills f ON f.planned_order_id = o.planned_order_id
            WHERE o.plan_id = ?
            GROUP BY o.planned_order_id
            """,
            (plan_id,),
        ).fetchall()
        completed = 0
        any_fill = False
        for order in orders:
            approved = int(order["approved_quantity"] or 0)
            filled = int(order["filled_quantity"] or 0)
            any_fill = any_fill or filled > 0
            if filled >= approved:
                status = "COMPLETED"
                completed += 1
            elif filled > 0:
                status = "PARTIAL"
            else:
                status = "APPROVED"
            connection.execute(
                "UPDATE planned_orders SET status = ? WHERE planned_order_id = ?",
                (status, int(order["planned_order_id"])),
            )
        if orders and completed == len(orders):
            plan_status = "COMPLETED"
            completed_at = _now()
        elif any_fill:
            plan_status = "PARTIAL"
            completed_at = None
        else:
            plan_status = "APPROVED"
            completed_at = None
        connection.execute(
            "UPDATE trade_plans SET status = ?, completed_at = ? WHERE plan_id = ?",
            (plan_status, completed_at, plan_id),
        )

    def _write_snapshot_and_replace(
        self,
        connection: sqlite3.Connection,
        account_id: int,
        as_of: str,
        cash_total: float,
        cash_available: float,
        cash_withdrawable: float,
        cash_frozen: float,
        positions: dict[str, dict],
        source: str,
        reconciliation_status: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO account_snapshots(
                account_id, as_of, cash_total, cash_available_to_trade,
                cash_withdrawable, cash_frozen, source,
                reconciliation_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                as_of,
                cash_total,
                cash_available,
                cash_withdrawable,
                cash_frozen,
                source,
                reconciliation_status,
                _now(),
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        connection.execute("DELETE FROM position_lots WHERE account_id = ?", (account_id,))
        for symbol, position in positions.items():
            total = int(position["total_quantity"])
            sellable = int(position["sellable_quantity"])
            cost_price = float(position.get("cost_price", 0.0))
            connection.execute(
                """
                INSERT INTO snapshot_positions(
                    snapshot_id, symbol, total_quantity, sellable_quantity,
                    frozen_quantity, cost_price, market_price, market_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    symbol,
                    total,
                    sellable,
                    int(position.get("frozen_quantity", 0)),
                    cost_price,
                    float(position.get("market_price", 0.0)),
                    float(position.get("market_value", 0.0)),
                ),
            )
            if sellable > 0:
                connection.execute(
                    """
                    INSERT INTO position_lots(
                        account_id, symbol, buy_trade_date, settle_date,
                        original_quantity, remaining_quantity, unit_cost,
                        source_snapshot_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (account_id, symbol, as_of, as_of, sellable, sellable, cost_price, snapshot_id),
                )
            unsettled = total - sellable
            if unsettled > 0:
                connection.execute(
                    """
                    INSERT INTO position_lots(
                        account_id, symbol, buy_trade_date, settle_date,
                        original_quantity, remaining_quantity, unit_cost,
                        source_snapshot_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
                    """,
                    (
                        account_id,
                        symbol,
                        as_of,
                        next_trade_date(as_of),
                        unsettled,
                        unsettled,
                        cost_price,
                        snapshot_id,
                    ),
                )
        connection.execute(
            """
            INSERT INTO account_balances(
                account_id, cash_total, cash_available_to_trade,
                cash_withdrawable, cash_frozen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                cash_total = excluded.cash_total,
                cash_available_to_trade = excluded.cash_available_to_trade,
                cash_withdrawable = excluded.cash_withdrawable,
                cash_frozen = excluded.cash_frozen,
                updated_at = excluded.updated_at
            """,
            (account_id, cash_total, cash_available, cash_withdrawable, cash_frozen, _now()),
        )
        return snapshot_id


def _normalize_positions(positions: dict[str, int | dict]) -> dict[str, dict]:
    normalized: dict[str, dict] = {}
    for raw_symbol, raw_position in positions.items():
        symbol = str(raw_symbol).zfill(6) if str(raw_symbol).isdigit() else str(raw_symbol)
        if isinstance(raw_position, dict):
            total = int(raw_position.get("total_quantity", raw_position.get("quantity", 0)))
            sellable = int(raw_position.get("sellable_quantity", total))
            frozen = int(raw_position.get("frozen_quantity", 0))
            cost_price = float(raw_position.get("cost_price", 0.0))
            market_price = float(raw_position.get("market_price", 0.0))
            market_value = float(raw_position.get("market_value", total * market_price))
        else:
            total = int(raw_position)
            sellable = total
            frozen = 0
            cost_price = 0.0
            market_price = 0.0
            market_value = 0.0
        if total < 0 or sellable < 0 or frozen < 0:
            raise ValueError(f"{symbol}: 持仓数量不能为负")
        if sellable > total:
            raise ValueError(f"{symbol}: 可卖数量不能超过总持仓")
        if total == 0:
            continue
        normalized[symbol] = {
            "total_quantity": total,
            "sellable_quantity": sellable,
            "frozen_quantity": frozen,
            "cost_price": cost_price,
            "market_price": market_price,
            "market_value": market_value,
        }
    return normalized


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    broker_name TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_balances (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(account_id),
    cash_total REAL NOT NULL,
    cash_available_to_trade REAL NOT NULL,
    cash_withdrawable REAL NOT NULL,
    cash_frozen REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    as_of TEXT NOT NULL,
    cash_total REAL NOT NULL,
    cash_available_to_trade REAL NOT NULL,
    cash_withdrawable REAL NOT NULL,
    cash_frozen REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_account_date
ON account_snapshots(account_id, as_of);

CREATE TABLE IF NOT EXISTS snapshot_positions (
    snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(snapshot_id),
    symbol TEXT NOT NULL,
    total_quantity INTEGER NOT NULL,
    sellable_quantity INTEGER NOT NULL,
    frozen_quantity INTEGER NOT NULL DEFAULT 0,
    cost_price REAL NOT NULL DEFAULT 0,
    market_price REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(snapshot_id, symbol)
);

CREATE TABLE IF NOT EXISTS trade_plans (
    plan_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    account_snapshot_id INTEGER REFERENCES account_snapshots(snapshot_id),
    strategy_name TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    intended_trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    source_run_id TEXT,
    config_fingerprint TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    completed_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_plans_date
ON trade_plans(account_id, intended_trade_date, status);

CREATE TABLE IF NOT EXISTS planned_orders (
    planned_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES trade_plans(plan_id),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy_quantity INTEGER NOT NULL,
    approved_quantity INTEGER,
    reference_price REAL NOT NULL,
    target_weight REAL NOT NULL DEFAULT 0,
    estimated_fee REAL NOT NULL DEFAULT 0,
    deferred_quantity INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    adjustment_reason TEXT
);

CREATE TABLE IF NOT EXISTS manual_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    planned_order_id INTEGER REFERENCES planned_orders(planned_order_id),
    broker_fill_id TEXT,
    trade_date TEXT NOT NULL,
    trade_time TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    other_fee REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, broker_fill_id)
);

CREATE TABLE IF NOT EXISTS position_lots (
    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    symbol TEXT NOT NULL,
    buy_trade_date TEXT NOT NULL,
    settle_date TEXT NOT NULL,
    original_quantity INTEGER NOT NULL,
    remaining_quantity INTEGER NOT NULL,
    unit_cost REAL NOT NULL,
    source_fill_id INTEGER REFERENCES manual_fills(fill_id),
    source_snapshot_id INTEGER REFERENCES account_snapshots(snapshot_id),
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_lots_account_symbol
ON position_lots(account_id, symbol, settle_date);

CREATE TABLE IF NOT EXISTS reconciliations (
    reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    as_of TEXT NOT NULL,
    broker_snapshot_id INTEGER REFERENCES account_snapshots(snapshot_id),
    status TEXT NOT NULL,
    cash_total_difference REAL NOT NULL,
    cash_available_difference REAL NOT NULL,
    position_difference_count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    resolution TEXT,
    confirmed_at TEXT
);
"""


__all__ = ["DEFAULT_ACCOUNT_NAME", "DEFAULT_DB_PATH", "TradingRepository", "next_trade_date"]
