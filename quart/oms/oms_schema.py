"""OMS 持久化 schema（OMS-001，TARGET_ARCHITECTURE_V3 §5.2）。

三张表，全部围绕"状态只能由 ExecutionReport 推进、成交不可重复入账"：

- `oms_orders`：订单镜像（`BrokerOrder` 查询模型的持久化形态），
  主键 `client_order_id`，`idempotency_key` 唯一 → 重复提交不产生重复订单；
- `oms_execution_reports`：事件去重索引 + 审计流水，`idempotency_key` 唯一
  → 重复回报幂等重放，不再次推进状态；
- `oms_fills`：真实成交账本，`idempotency_key` 与 `event_id` 唯一
  → 同一回报重复到达不会重复入账。

成交入账与订单状态更新在同一事务内完成；即使进程在两者之间崩溃，
重启后重放回报也只会得到同一份账本（验收：重复回报/重启不重复入账）。
"""
from __future__ import annotations

import sqlite3

from quart.infrastructure.db import Migration


def _up_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS oms_orders (
            client_order_id TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            requested_quantity INTEGER NOT NULL,
            approved_quantity INTEGER NOT NULL,
            limit_price TEXT,
            planned_order_id TEXT,
            status TEXT NOT NULL,
            broker_order_id TEXT,
            filled_quantity INTEGER NOT NULL DEFAULT 0,
            average_fill_price TEXT NOT NULL DEFAULT '0',
            status_reason TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            business_time TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_event_id TEXT,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_oms_orders_account
            ON oms_orders(account_id, status, created_at);

        CREATE TABLE IF NOT EXISTS oms_execution_reports (
            event_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            client_order_id TEXT NOT NULL
                REFERENCES oms_orders(client_order_id),
            intent_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            cumulative_filled_quantity INTEGER NOT NULL,
            last_filled_quantity INTEGER NOT NULL DEFAULT 0,
            last_fill_price TEXT,
            average_fill_price TEXT,
            broker_order_id TEXT,
            reason TEXT NOT NULL DEFAULT '',
            business_time TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_oms_reports_order
            ON oms_execution_reports(client_order_id, created_at);

        CREATE TABLE IF NOT EXISTS oms_fills (
            fill_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            event_id TEXT NOT NULL UNIQUE,
            client_order_id TEXT NOT NULL
                REFERENCES oms_orders(client_order_id),
            intent_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price TEXT NOT NULL,
            commission TEXT NOT NULL DEFAULT '0',
            stamp_tax TEXT NOT NULL DEFAULT '0',
            transfer_fee TEXT NOT NULL DEFAULT '0',
            other_fee TEXT NOT NULL DEFAULT '0',
            broker_order_id TEXT,
            broker_fill_id TEXT,
            planned_order_id TEXT,
            business_time TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_oms_fills_account
            ON oms_fills(account_id, business_time);
        """
    )


def _down_v3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS oms_fills;
        DROP TABLE IF EXISTS oms_execution_reports;
        DROP TABLE IF EXISTS oms_orders;
        """
    )


OMS_MIGRATIONS: list[Migration] = [
    Migration(version=3, name="oms_orders_reports_fills", up=_up_v3, down=_down_v3),
]

__all__ = ["OMS_MIGRATIONS"]
