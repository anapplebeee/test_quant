"""核心指标采集与查询（OBS-001，TARGET_ARCHITECTURE_V3 §13.2）。

两类指标：

- **自定义指标**：`MetricsRepository.record` 写入 `obs_metrics` 表
  （v4 migration），供调用方记录无法从既有表派生的观测值；
- **派生指标**：`collect_core_metrics` 直接对平台表（jobs / oms / risk）
  做聚合——指标永远与数据库一致，没有"采集漂移"风险。

覆盖 §13.2 核心指标中可从单机平台表派生的部分：
Job 排队/运行时长、失败率与重试次数；委托拒绝率、成交率、部分成交、
未解决订单数；风控拒绝与状态切换。数据新鲜度与质量阻断由数据面
（`data_health` / 质量闸门）既有路径提供；对账差异在对账能力落地后补充。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from quart.infrastructure.db import Database


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class MetricsRepository:
    """自定义指标的持久化仓库。测试可传独立临时库。"""

    def __init__(self, db: Database | None = None):
        if db is None:
            from quart.infrastructure.db import get_db

            db = get_db()
        self.db: Database = db
        self._lock = threading.Lock()

    def migrate(self) -> list[int]:
        from quart.infrastructure.migrations import PLATFORM_MIGRATIONS

        return self.db.apply(PLATFORM_MIGRATIONS)

    def record(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """记录一条指标观测值。"""
        if not str(name).strip():
            raise ValueError("指标名不能为空")
        self.migrate()
        with self._lock, self.db.connect() as conn:
            conn.execute(
                "INSERT INTO obs_metrics (name, labels_json, value, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(name).strip(),
                    json.dumps(labels or {}, ensure_ascii=False, sort_keys=True),
                    float(value),
                    _now_iso(),
                ),
            )
            conn.commit()

    def latest(self, name: str) -> tuple[float, dict[str, str]] | None:
        """最近一条观测值，返回 ``(value, labels)``；无记录返回 None。"""
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT value, labels_json FROM obs_metrics WHERE name = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return float(row["value"]), json.loads(row["labels_json"] or "{}")

    def history(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        """按时间倒序返回指标历史。"""
        self.migrate()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT value, labels_json, created_at FROM obs_metrics "
                "WHERE name = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (name, int(limit)),
            ).fetchall()
        return [
            {
                "value": float(r["value"]),
                "labels": json.loads(r["labels_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def _safe_query(db: Database, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """表不存在时返回空结果（允许部分模块未初始化的库）。"""
    try:
        with db.connect() as conn:
            return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _job_metrics(db: Database) -> dict[str, Any]:
    rows = _safe_query(db, "SELECT status, created_at, started_at, finished_at, attempts FROM jobs")
    total = len(rows)
    if total == 0:
        return {"jobs_total": 0}
    by_status: dict[str, int] = {}
    queue_seconds: list[float] = []
    run_seconds: list[float] = []
    retries = 0
    for r in rows:
        status = str(r["status"])
        by_status[status] = by_status.get(status, 0) + 1
        retries += max(0, int(r["attempts"]) - 1)
        created = _parse_ts(r["created_at"])
        started = _parse_ts(r["started_at"])
        finished_at_ts = _parse_ts(r["finished_at"])
        if created and started:
            queue_seconds.append((started - created).total_seconds())
        if started and finished_at_ts:
            run_seconds.append((finished_at_ts - started).total_seconds())
    finished_count = sum(
        by_status.get(s, 0) for s in ("SUCCEEDED", "FAILED")
    )
    failed = by_status.get("FAILED", 0)
    return {
        "jobs_total": total,
        "jobs_active": sum(
            by_status.get(s, 0) for s in ("CREATED", "QUEUED", "CLAIMED", "RUNNING")
        ),
        "jobs_succeeded": by_status.get("SUCCEEDED", 0),
        "jobs_failed": failed,
        "jobs_cancelled": by_status.get("CANCELLED", 0),
        "job_failure_rate": (failed / finished_count) if finished_count else 0.0,
        "job_avg_queue_seconds": (
            sum(queue_seconds) / len(queue_seconds) if queue_seconds else 0.0
        ),
        "job_avg_run_seconds": (
            sum(run_seconds) / len(run_seconds) if run_seconds else 0.0
        ),
        "job_total_retries": retries,
    }


def _order_metrics(db: Database) -> dict[str, Any]:
    rows = _safe_query(
        db, "SELECT status, requested_quantity, filled_quantity FROM oms_orders"
    )
    total = len(rows)
    if total == 0:
        return {"orders_total": 0}
    by_status: dict[str, int] = {}
    for r in rows:
        status = str(r["status"])
        by_status[status] = by_status.get(status, 0) + 1
    denied = by_status.get("DENIED", 0)
    rejected = by_status.get("REJECTED", 0)
    filled = by_status.get("FILLED", 0)
    partial = by_status.get("PARTIALLY_FILLED", 0)
    active = sum(
        by_status.get(s, 0)
        for s in ("CREATED", "RISK_APPROVED", "SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED")
    )
    return {
        "orders_total": total,
        "orders_denied": denied,
        "orders_rejected": rejected,
        "order_reject_rate": (denied + rejected) / total,
        "orders_filled": filled,
        "order_fill_rate": filled / total,
        "orders_partially_filled": partial,
        "orders_active": active,
    }


def _risk_metrics(db: Database) -> dict[str, Any]:
    decision_rows = _safe_query(db, "SELECT status FROM risk_decisions")
    history_rows = _safe_query(db, "SELECT 1 FROM risk_state_history")
    total = len(decision_rows)
    denied = sum(1 for r in decision_rows if str(r["status"]) == "DENY")
    return {
        "risk_decisions_total": total,
        "risk_decisions_denied": denied,
        "risk_state_transitions": len(history_rows),
    }


def collect_core_metrics(db: Database | None = None) -> dict[str, Any]:
    """汇总平台核心指标（派生自 jobs / oms / risk 表）。

    返回值按主题分块：``job`` / ``order`` / ``risk``，外加采集时间戳。
    """
    if db is None:
        from quart.infrastructure.db import get_db

        db = get_db()
    return {
        "collected_at": _now_iso(),
        "job": _job_metrics(db),
        "order": _order_metrics(db),
        "risk": _risk_metrics(db),
    }


__all__ = ["MetricsRepository", "collect_core_metrics"]
