"""持久化订单状态机与成交入账仓储（OMS-001）。

核心不变量
--------
1. 订单状态只能由 `ExecutionReport` 推进（领域层 `apply_execution_report`
   负责转换校验，仓储负责持久化）；
2. 回报按 `idempotency_key` 去重：重复回报是幂等重放，返回当前订单，
   不再次推进状态、不再次入账；
3. 成交按 `event_id` / `idempotency_key` 唯一：订单状态更新、回报记录
   与成交入账在同一事务提交——进程在任意点崩溃，重启重放只会得到
   同一份账本（验收：重复回报/重启不重复入账）；
4. 订单按 `idempotency_key` 幂等创建：重复提交返回既有订单。

重启恢复
--------
`list_active_orders()` 返回全部非终态订单。网络超时不是失败结论：
处于 `SUBMITTING` 的订单必须先按 `client_order_id` 向券商查询，
再决定重放哪一条回报（该查询能力由 Broker Adapter 提供，见 BROKER-001）。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from quart.domain.enums import OrderStatus
from quart.domain.executions import ExecutionReport, Fill
from quart.domain.ids import stable_id
from quart.domain.orders import BrokerOrder
from quart.domain.state_machine import apply_execution_report
from quart.infrastructure.db import Database

_FILL_STATUSES = (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class OrderRepository:
    """订单、回报与成交的持久化仓储。测试可传独立临时库。"""

    def __init__(self, db: Database | None = None):
        if db is None:
            from quart.infrastructure.db import get_db

            db = get_db()
        self.db: Database = db
        self._lock = threading.Lock()

    def migrate(self) -> list[int]:
        """应用平台 schema migration（全量）。"""
        from quart.infrastructure.migrations import PLATFORM_MIGRATIONS

        return self.db.apply(PLATFORM_MIGRATIONS)

    # ---------------- 订单 ----------------

    def create_order(self, order: BrokerOrder) -> BrokerOrder:
        """落库一笔订单；幂等键或主键已存在时返回既有订单（不产生重复）。"""
        self.migrate()
        with self._lock, self.db.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO oms_orders (
                        client_order_id, intent_id, account_id, environment,
                        symbol, side, requested_quantity, approved_quantity,
                        limit_price, planned_order_id, status, broker_order_id,
                        filled_quantity, average_fill_price, status_reason,
                        idempotency_key, business_time, source,
                        created_at, updated_at, last_event_id, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._order_row(order),
                )
                conn.commit()
                return order
            except sqlite3.IntegrityError:
                conn.rollback()
        existing = self.get_order_by_key(order.idempotency_key) or self.get_order(
            order.client_order_id
        )
        if existing is None:
            raise ValueError(f"订单创建冲突但无法定位既有记录: {order.client_order_id}")
        self._require_same_identity(existing, order)
        return existing

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oms_orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._order_from_row(row) if row else None

    def get_order_by_key(self, idempotency_key: str) -> BrokerOrder | None:
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oms_orders WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._order_from_row(row) if row else None

    def list_orders(
        self,
        account_id: str | None = None,
        only_active: bool = False,
        limit: int = 200,
    ) -> list[BrokerOrder]:
        self.migrate()
        sql = "SELECT * FROM oms_orders"
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if only_active:
            terminal = ", ".join(f"'{s.value}'" for s in (
                OrderStatus.FILLED, OrderStatus.CANCELED,
                OrderStatus.REJECTED, OrderStatus.DENIED,
            ))
            clauses.append(f"status NOT IN ({terminal})")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [o for o in (self._order_from_row(r) for r in rows) if o is not None]

    def list_active_orders(self, account_id: str | None = None) -> list[BrokerOrder]:
        """重启恢复入口：全部非终态订单。"""
        return self.list_orders(account_id=account_id, only_active=True, limit=1000)

    # ---------------- 回报与成交入账 ----------------

    def apply_report(self, report: ExecutionReport) -> BrokerOrder:
        """推进订单状态并（如有成交）入账。

        - 重复回报（同 `idempotency_key`）幂等重放：返回当前订单，不重复入账；
        - 非法转换抛 `OrderTransitionError`，不落任何行；
        - 订单更新 + 回报记录 + 成交入账在同一事务提交。
        """
        self.migrate()
        with self._lock, self.db.connect() as conn:
            replay = conn.execute(
                "SELECT 1 FROM oms_execution_reports WHERE idempotency_key = ?",
                (report.idempotency_key,),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM oms_orders WHERE client_order_id = ?",
                (report.client_order_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"订单不存在: {report.client_order_id}")
            order = self._order_from_row(row)
            assert order is not None
            if replay:
                return order
            updated = apply_execution_report(order, report)
            fill = self._fill_from_report(updated, report)
            try:
                conn.execute(
                    """
                    UPDATE oms_orders
                    SET status = ?, broker_order_id = ?, filled_quantity = ?,
                        average_fill_price = ?, status_reason = ?,
                        updated_at = ?, last_event_id = ?, version = ?
                    WHERE client_order_id = ?
                    """,
                    (
                        updated.status.value,
                        updated.broker_order_id,
                        updated.filled_quantity,
                        str(updated.average_fill_price),
                        updated.status_reason,
                        updated.updated_at.isoformat() if updated.updated_at else None,
                        updated.last_event_id,
                        updated.version,
                        report.client_order_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO oms_execution_reports (
                        event_id, idempotency_key, client_order_id, intent_id,
                        account_id, environment, status, cumulative_filled_quantity,
                        last_filled_quantity, last_fill_price, average_fill_price,
                        broker_order_id, reason, business_time, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.event_id,
                        report.idempotency_key,
                        report.client_order_id,
                        report.intent_id,
                        report.account_id,
                        report.environment.value,
                        report.status.value,
                        report.cumulative_filled_quantity,
                        report.last_filled_quantity,
                        str(report.last_fill_price) if report.last_fill_price is not None else None,
                        str(report.average_fill_price) if report.average_fill_price is not None else None,
                        report.broker_order_id,
                        report.reason,
                        report.business_time.isoformat(),
                        report.source,
                        report.created_at.isoformat(),
                    ),
                )
                if fill is not None:
                    conn.execute(
                        """
                        INSERT INTO oms_fills (
                            fill_id, idempotency_key, event_id, client_order_id,
                            intent_id, account_id, environment, symbol, side,
                            quantity, price, commission, stamp_tax, transfer_fee,
                            other_fee, broker_order_id, broker_fill_id,
                            planned_order_id, business_time, source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill.fill_id,
                            fill.idempotency_key,
                            fill.event_id,
                            fill.client_order_id,
                            fill.intent_id,
                            fill.account_id,
                            fill.environment.value,
                            fill.symbol,
                            fill.side.value,
                            fill.quantity,
                            str(fill.price),
                            str(fill.commission),
                            str(fill.stamp_tax),
                            str(fill.transfer_fee),
                            str(fill.other_fee),
                            fill.broker_order_id,
                            fill.broker_fill_id,
                            fill.planned_order_id,
                            fill.business_time.isoformat(),
                            fill.source,
                            fill.created_at.isoformat(),
                        ),
                    )
                conn.commit()
            except sqlite3.IntegrityError:
                # 并发下同键回报抢先落库：重放语义，返回当前订单
                conn.rollback()
                replayed = self.get_order(report.client_order_id)
                return replayed if replayed is not None else updated
        return updated

    def list_reports(self, client_order_id: str) -> list[dict[str, Any]]:
        """单笔订单的回报审计流水（按时间正序）。"""
        self.migrate()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, idempotency_key, status, cumulative_filled_quantity,
                       last_filled_quantity, last_fill_price, broker_order_id,
                       reason, business_time, source
                FROM oms_execution_reports
                WHERE client_order_id = ?
                ORDER BY created_at ASC, event_id ASC
                """,
                (client_order_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_fills(
        self,
        account_id: str | None = None,
        client_order_id: str | None = None,
        limit: int = 500,
    ) -> list[Fill]:
        self.migrate()
        sql = "SELECT * FROM oms_fills"
        clauses: list[str] = []
        params: list[Any] = []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if client_order_id:
            clauses.append("client_order_id = ?")
            params.append(client_order_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY business_time ASC LIMIT ?"
        params.append(int(limit))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [f for f in (self._fill_from_row(r) for r in rows) if f is not None]

    def positions_from_fills(self, account_id: str) -> dict[str, int]:
        """由成交推导的持仓查询模型（PositionSnapshot 语义：只读、不入账）。

        这是派生视图，不是账户权威源；真实对账仍以券商查询为准。
        """
        self.migrate()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, side, quantity FROM oms_fills "
                "WHERE account_id = ? ORDER BY business_time ASC",
                (account_id,),
            ).fetchall()
        positions: dict[str, int] = {}
        for r in rows:
            delta = int(r["quantity"]) if r["side"] == "BUY" else -int(r["quantity"])
            positions[r["symbol"]] = positions.get(r["symbol"], 0) + delta
        return {symbol: qty for symbol, qty in positions.items() if qty != 0}

    # ---------------- 序列化 ----------------

    @staticmethod
    def _order_row(order: BrokerOrder) -> tuple:
        return (
            order.client_order_id,
            order.intent_id,
            order.account_id,
            order.environment.value,
            order.symbol,
            order.side.value,
            order.requested_quantity,
            order.approved_quantity,
            str(order.limit_price) if order.limit_price is not None else None,
            order.planned_order_id,
            order.status.value,
            order.broker_order_id,
            order.filled_quantity,
            str(order.average_fill_price),
            order.status_reason,
            order.idempotency_key,
            order.business_time.isoformat(),
            order.source,
            order.created_at.isoformat(),
            order.updated_at.isoformat() if order.updated_at else order.business_time.isoformat(),
            order.last_event_id,
            order.version,
        )

    @staticmethod
    def _require_same_identity(existing: BrokerOrder, incoming: BrokerOrder) -> None:
        for field_name in ("intent_id", "symbol", "side", "requested_quantity"):
            if getattr(existing, field_name) != getattr(incoming, field_name):
                raise ValueError(
                    f"同一订单键的委托内容不一致: {field_name} "
                    f"{getattr(existing, field_name)} != {getattr(incoming, field_name)}"
                )

    @staticmethod
    def _order_from_row(row) -> BrokerOrder | None:
        d = dict(row)
        try:
            return BrokerOrder(
                client_order_id=d["client_order_id"],
                intent_id=d["intent_id"],
                account_id=d["account_id"],
                environment=d["environment"],
                symbol=d["symbol"],
                side=d["side"],
                requested_quantity=int(d["requested_quantity"]),
                approved_quantity=int(d["approved_quantity"]),
                limit_price=d["limit_price"],
                planned_order_id=d["planned_order_id"],
                status=d["status"],
                broker_order_id=d["broker_order_id"],
                filled_quantity=int(d["filled_quantity"]),
                average_fill_price=d["average_fill_price"],
                status_reason=d["status_reason"],
                idempotency_key=d["idempotency_key"],
                business_time=_parse_dt(d["business_time"]),
                source=d["source"],
                created_at=_parse_dt(d["created_at"]),
                updated_at=_parse_dt(d["updated_at"]),
                last_event_id=d["last_event_id"],
                version=int(d["version"]),
            )
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _fill_from_report(order: BrokerOrder, report: ExecutionReport) -> Fill | None:
        """成交回报 → 入账 Fill；非成交回报返回 None。"""
        if report.status not in _FILL_STATUSES:
            return None
        price = report.last_fill_price
        if price is None:
            price = report.average_fill_price
        assert price is not None
        fill_key = f"oms-fill:{report.event_id}"
        return Fill.create(
            fill_id=stable_id("fill", f"{order.account_id}:{fill_key}"),
            event_id=report.event_id,
            client_order_id=order.client_order_id,
            intent_id=order.intent_id,
            account_id=order.account_id,
            environment=order.environment,
            symbol=order.symbol,
            side=order.side,
            quantity=report.last_filled_quantity,
            price=price,
            business_time=report.business_time,
            source=report.source,
            idempotency_key=f"{order.account_id}:{fill_key}",
            broker_order_id=report.broker_order_id,
            planned_order_id=order.planned_order_id,
        )

    @staticmethod
    def _fill_from_row(row) -> Fill | None:
        d = dict(row)
        try:
            return Fill(
                fill_id=d["fill_id"],
                event_id=d["event_id"],
                client_order_id=d["client_order_id"],
                intent_id=d["intent_id"],
                account_id=d["account_id"],
                environment=d["environment"],
                symbol=d["symbol"],
                side=d["side"],
                quantity=int(d["quantity"]),
                price=d["price"],
                business_time=_parse_dt(d["business_time"]),
                source=d["source"],
                idempotency_key=d["idempotency_key"],
                broker_order_id=d["broker_order_id"],
                broker_fill_id=d["broker_fill_id"],
                planned_order_id=d["planned_order_id"],
                commission=d["commission"],
                stamp_tax=d["stamp_tax"],
                transfer_fee=d["transfer_fee"],
                other_fee=d["other_fee"],
                created_at=_parse_dt(d["created_at"]),
            )
        except (KeyError, ValueError):
            return None


__all__ = ["OrderRepository"]
