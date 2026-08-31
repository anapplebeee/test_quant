"""持久化 Job 模型与仓库（JOB-001）。

解决进程内 TaskQueue 的问题：进程重启丢失、无 claim/lease/recovery/幂等。

核心能力（对齐 ADR-0001 验收标准）：
- create：创建 job，幂等键唯一（重复提交返回原 job）；
- claim：Worker 原子认领 QUEUED job（CAS 语义），分配租约；
- lease/heartbeat：续约，防 Worker 假死；
- recovery：进程重启后，lease 过期的 CLAIMED/RUNNING job 重置为 QUEUED（重试）
  或超限标记 FAILED；
- cancel：取消未终态 job；
- 状态机：CREATED→QUEUED→CLAIMED→RUNNING→SUCCEEDED/FAILED/CANCELLED。
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from quart.infrastructure.db import Database
from quart.infrastructure.job_schema import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_CREATED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)
from quart.infrastructure.migrations import PLATFORM_MIGRATIONS


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    """持久化任务的不可变视图。"""

    job_id: str
    job_type: str
    status: str
    idempotency_key: str | None = None
    payload: dict = field(default_factory=dict)
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    claimed_by: str | None = None
    lease_until: str | None = None
    result: dict | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row) -> Job:
        d = dict(row)
        result_raw = d.get("result_json")
        return cls(
            job_id=str(d["job_id"]),
            job_type=str(d["job_type"]),
            status=str(d["status"]),
            idempotency_key=d.get("idempotency_key"),
            payload=json.loads(d.get("payload_json") or "{}"),
            priority=int(d.get("priority") or 0),
            attempts=int(d.get("attempts") or 0),
            max_attempts=int(d.get("max_attempts") or 3),
            created_at=str(d.get("created_at") or ""),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            claimed_by=d.get("claimed_by"),
            lease_until=d.get("lease_until"),
            result=json.loads(result_raw) if result_raw else None,
            error=d.get("error"),
        )


class JobRepository:
    """基于 SQLite 的持久化 Job 仓库。

    Parameters
    ----------
    db:
        数据库实例。默认用平台库，测试可传独立临时库。
    lease_seconds:
        单次租约时长。Worker 必须在到期前续约，否则 recovery 会回收。
    """

    def __init__(self, db: Database | None = None, lease_seconds: int = 300):
        if db is None:
            from quart.infrastructure.db import get_db

            db = get_db()
        self.db: Database = db
        self.lease_seconds = lease_seconds
        self._lock = threading.Lock()

    def migrate(self) -> list[int]:
        """应用平台 schema migration（全量，保证任意初始化顺序下表都齐全）。"""
        return self.db.apply(PLATFORM_MIGRATIONS)

    # ---------------- Create ----------------

    def create(
        self,
        job_type: str,
        payload: dict | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> Job:
        """创建 job 并入队（CREATED→QUEUED）。

        幂等：idempotency_key 唯一。重复提交同一 key 返回已存在的 job，
        不产生新记录。
        """
        self.migrate()
        job_id = uuid.uuid4().hex
        now = _now()
        with self.db.connect() as conn:
            # 幂等：先查同 key 已存在的 job
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    return Job.from_row(existing)
            conn.execute(
                """
                INSERT INTO jobs(
                    job_id, idempotency_key, job_type, payload_json, status,
                    priority, attempts, max_attempts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    job_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    JOB_CREATED,
                    int(priority),
                    int(max_attempts),
                    now,
                ),
            )
            # 入队
            conn.execute(
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                (JOB_QUEUED, job_id),
            )
        return self.get(job_id)  # type: ignore[return-value]

    # ---------------- Claim / Lease ----------------

    def claim(self, worker_id: str, job_types: list[str] | None = None) -> Job | None:
        """原子认领一个 QUEUED job，分配租约。

        CAS 语义：`UPDATE ... WHERE status='QUEUED' AND id=...` 只有一行被改，
        多 Worker 并发 claim 不会抢到同一个 job。
        """
        self.migrate()
        lease_until = _now_plus(self.lease_seconds)
        with self.db.connect() as conn:
            type_filter = ""
            params: list[Any] = []
            if job_types:
                placeholders = ",".join("?" for _ in job_types)
                type_filter = f" AND job_type IN ({placeholders})"
                params.extend(job_types)
            # 挑一个可认领的 job（最高优先级、最早创建）
            candidate = conn.execute(
                f"""
                SELECT job_id FROM jobs
                WHERE status IN ('{JOB_CREATED}', '{JOB_QUEUED}') {type_filter}
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if candidate is None:
                return None
            job_id = str(candidate["job_id"])
            # CAS：仅当仍是可认领状态时更新为 CLAIMED
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, claimed_by = ?, lease_until = ?,
                    attempts = attempts + 1, started_at = ?
                WHERE job_id = ? AND status IN ('{JOB_CREATED}', '{JOB_QUEUED}')
                """,
                (JOB_CLAIMED, worker_id, lease_until, _now(), job_id),
            )
            if cur.rowcount == 0:
                return None  # 被其他 Worker 抢走
        return self.get(job_id)

    def claim_job(self, job_id: str, worker_id: str) -> Job | None:
        """原子认领**指定** job（已知 job_id 的调用方，如 task_api 水合恢复）。

        CAS 语义与 `claim()` 相同；状态非可认领时返回 None。
        """
        self.migrate()
        lease_until = _now_plus(self.lease_seconds)
        with self.db.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, claimed_by = ?, lease_until = ?,
                    attempts = attempts + 1, started_at = ?
                WHERE job_id = ? AND status IN ('{JOB_CREATED}', '{JOB_QUEUED}')
                """,
                (JOB_CLAIMED, worker_id, lease_until, _now(), job_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get(job_id)

    def mark_running(self, job_id: str, worker_id: str, lease_seconds: int | None = None) -> bool:
        """Worker 确认接手，job 进入 RUNNING，并续租。"""
        lease = _now_plus(lease_seconds or self.lease_seconds)
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status = ?, lease_until = ?
                WHERE job_id = ? AND claimed_by = ? AND status = ?
                """,
                (JOB_RUNNING, lease, job_id, worker_id, JOB_CLAIMED),
            )
            return cur.rowcount > 0

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Worker 心跳续约。"""
        lease = _now_plus(self.lease_seconds)
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs
                SET lease_until = ?
                WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
                """,
                (lease, job_id, worker_id, JOB_CLAIMED, JOB_RUNNING),
            )
            return cur.rowcount > 0

    # ---------------- 完成 / 失败 / 取消 ----------------

    def succeed(self, job_id: str, worker_id: str, result: dict | None = None) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs SET status = ?, result_json = ?, finished_at = ?,
                    lease_until = NULL
                WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
                """,
                (JOB_SUCCEEDED, json.dumps(result or {}), _now(), job_id, worker_id,
                 JOB_CLAIMED, JOB_RUNNING),
            )
            return cur.rowcount > 0

    def fail(self, job_id: str, worker_id: str, error: str) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs SET status = ?, error = ?, finished_at = ?,
                    lease_until = NULL
                WHERE job_id = ? AND claimed_by = ? AND status IN (?, ?)
                """,
                (JOB_FAILED, error, _now(), job_id, worker_id, JOB_CLAIMED, JOB_RUNNING),
            )
            return cur.rowcount > 0

    def cancel(self, job_id: str, reason: str | None = None) -> bool:
        """取消未终态 job（含排队中、租约中）。"""
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                UPDATE jobs SET status = ?, finished_at = ?, lease_until = NULL,
                    error = COALESCE(?, error)
                WHERE job_id = ? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')
                """,
                (JOB_CANCELLED, _now(), reason, job_id),
            )
            return cur.rowcount > 0

    # ---------------- Recovery ----------------

    def recover(self, worker_id: str = "__recovery__") -> dict[str, int]:
        """恢复：将租约过期的 CLAIMED/RUNNING job 重置为 QUEUED（重试）或 FAILED（超限）。

        返回 {"requeued": n, "failed": n, "cancelled": n}。

        进程崩溃后，Worker 持有的租约不再续约，lease_until 过期即视为失联。
        未超过 max_attempts 的 job 重新入队；超过的标记 FAILED。
        """
        self.migrate()
        requeued = failed = 0
        now = _now()
        with self.db.connect() as conn:
            stale = conn.execute(
                """
                SELECT job_id, attempts, max_attempts FROM jobs
                WHERE status IN ('CLAIMED','RUNNING') AND lease_until IS NOT NULL
                  AND lease_until < ?
                """,
                (now,),
            ).fetchall()
            for row in stale:
                job_id = str(row["job_id"])
                attempts = int(row["attempts"])
                max_attempts = int(row["max_attempts"])
                if attempts < max_attempts:
                    conn.execute(
                        """
                        UPDATE jobs SET status = ?, claimed_by = NULL, lease_until = NULL
                        WHERE job_id = ? AND status IN ('CLAIMED','RUNNING')
                        """,
                        (JOB_QUEUED, job_id),
                    )
                    requeued += 1
                else:
                    conn.execute(
                        """
                        UPDATE jobs SET status = ?, finished_at = ?, lease_until = NULL,
                            error = ?
                        WHERE job_id = ? AND status IN ('CLAIMED','RUNNING')
                        """,
                        (JOB_FAILED, _now(), f"recovery: attempts {attempts} >= max {max_attempts}",
                         job_id),
                    )
                    failed += 1
        return {"requeued": requeued, "failed": failed, "cancelled": 0}

    # ---------------- Query ----------------

    def get(self, job_id: str) -> Job | None:
        if not self.db.path.exists():
            return None
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return Job.from_row(row) if row else None

    def get_by_idempotency_key(self, key: str) -> Job | None:
        self.migrate()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
            return Job.from_row(row) if row else None

    def list(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        if not self.db.path.exists():
            return []
        self.migrate()
        sql = "SELECT * FROM jobs"
        conds: list[str] = []
        params: list[Any] = []
        if status:
            conds.append("status = ?")
            params.append(status)
        if job_type:
            conds.append("job_type = ?")
            params.append(job_type)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [Job.from_row(r) for r in rows]


def _now_plus(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


__all__ = ["Job", "JobRepository"]
