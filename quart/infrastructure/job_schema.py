"""Job 表 schema migration（JOB-001，版本 1）。

按 ADR-0001：只含**通用平台字段**，不含 ARCH-001 待冻结的订单/风控领域列。

Job 状态机：
    CREATED → QUEUED → CLAIMED → RUNNING → SUCCEEDED
                              ↘ FAILED
                              ↘ CANCELLED

关键字段：
- `idempotency_key`：幂等键，唯一约束。重复提交同一 job 返回原 job，不产生重复。
- `lease_until`：租约到期时间。Worker 周期心跳续约。
- `attempts`：已尝试次数，超过 max_attempts 后 recovery 不再重试，标记 FAILED。
- `payload_json` / `result_json`：任务参数与结果的 JSON 序列化（平台通用，不解析领域字段）。
"""
from __future__ import annotations

import sqlite3

from quart.infrastructure.db import Migration

#: Job 状态
JOB_CREATED = "CREATED"
JOB_QUEUED = "QUEUED"
JOB_CLAIMED = "CLAIMED"
JOB_RUNNING = "RUNNING"
JOB_SUCCEEDED = "SUCCEEDED"
JOB_FAILED = "FAILED"
JOB_CANCELLED = "CANCELLED"

#: 可被 claim 的状态
CLAIMABLE = (JOB_CREATED, JOB_QUEUED)
#: 终态
TERMINAL = (JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED)


def _up_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            job_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'CREATED',
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            claimed_by TEXT,
            lease_until TEXT,
            result_json TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs(status, priority DESC, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_lease
            ON jobs(lease_until);
        """
    )


def _down_v1(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS jobs")


#: 全部 Job migration（后续 schema 变更在此追加新版本）
JOB_MIGRATIONS: list[Migration] = [
    Migration(version=1, name="job_base_schema", up=_up_v1, down=_down_v1),
]

__all__ = [
    "CLAIMABLE",
    "JOB_CANCELLED",
    "JOB_CLAIMED",
    "JOB_CREATED",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RUNNING",
    "JOB_SUCCEEDED",
    "JOB_MIGRATIONS",
    "TERMINAL",
]
