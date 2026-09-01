"""Risk 表 schema migration（RISK-001，版本 2）。

按 TARGET_ARCHITECTURE_V3 §9：
- 风险状态机 `ACTIVE → REDUCING → HALTED → RECOVERY → ACTIVE` 持久化；
- 风险决策必须保存规则版本、输入、调整结果和原因（审计回放）。

表设计
------
- `risk_states`：每账户最新风险状态（单一权威行，upsert）。
- `risk_state_history`：状态切换审计流水（append-only，含操作者与原因）。
- `risk_decisions`：风控决策记录。`idempotency_key` 唯一，重试同一意图
  返回同一决策而不是重复评估（与 ARCH-001 RiskDecision 幂等键一致）。
"""
from __future__ import annotations

import sqlite3

from quart.infrastructure.db import Migration


def _up_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS risk_states (
            account_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            reason TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT '',
            limit_version TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_state_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_risk_state_history_account
            ON risk_state_history(account_id, history_id DESC);
        CREATE TABLE IF NOT EXISTS risk_decisions (
            decision_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            intent_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_quantity INTEGER NOT NULL,
            approved_quantity INTEGER NOT NULL,
            rules_json TEXT NOT NULL DEFAULT '[]',
            limit_version TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            business_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_risk_decisions_account
            ON risk_decisions(account_id, created_at DESC);
        """
    )


def _down_v2(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS risk_decisions")
    conn.execute("DROP TABLE IF EXISTS risk_state_history")
    conn.execute("DROP TABLE IF EXISTS risk_states")


def _up_v5(conn: sqlite3.Connection) -> None:
    """RISK-002：日损的日初权益、评估结果与触发状态审计。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS risk_daily_equity_marks (
            account_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            opening_equity REAL NOT NULL,
            current_equity REAL NOT NULL,
            daily_loss_pct REAL NOT NULL,
            baseline_date TEXT,
            baseline_available INTEGER NOT NULL DEFAULT 0,
            limit_version TEXT NOT NULL,
            triggered_state TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_risk_daily_equity_marks_account_date
            ON risk_daily_equity_marks(account_id, trade_date DESC);
        ALTER TABLE risk_state_history
            ADD COLUMN limit_version TEXT NOT NULL DEFAULT '';
        """
    )


def _down_v5(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS risk_daily_equity_marks")


#: 全部 Risk migration（后续 schema 变更在此追加新版本）
RISK_MIGRATIONS: list[Migration] = [
    Migration(version=2, name="risk_state_and_decisions", up=_up_v2, down=_down_v2),
    Migration(version=5, name="risk_daily_equity_marks", up=_up_v5, down=_down_v5),
]

__all__ = ["RISK_MIGRATIONS"]
