"""JOB 恢复测试夹具（协调文档 10.2：Job/Worker 重启恢复测试）。

提供可复用的崩溃/恢复场景，供多个测试引用。核心场景：

1. `crash_after_running(repo, worker)`：
   Worker 认领并标记 RUNNING 后**模拟进程崩溃**（不调用 heartbeat/succeed/fail，
   直接丢弃引用）→ 租约在 lease_seconds 后过期。

2. `simulate_crash_and_recover(repo, db)`：
   完整"崩溃 → 新进程 recover → 重新 claim 执行"流程。

用法：测试里 `from tests.job_recovery_fixtures import ...`。
注意：本模块是测试夹具，不进入生产代码。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.infrastructure.job_schema import JOB_RUNNING, JOB_SUCCEEDED


def crash_after_running(repo: JobRepository, worker: str = "worker-1"):
    """创建 job → claim → 标记 RUNNING → 模拟崩溃（租约过期）。

    Returns
    -------
    job_id : 崩溃前认领的 job（租约已过期，可被 recovery 回收）。
    """
    job = repo.create("backtest", {"strategy": "lowvol_indz"}, max_attempts=3)
    claimed = repo.claim(worker)
    repo.mark_running(claimed.job_id, worker)
    # 模拟进程崩溃：直接丢弃 worker 引用，不续约。租约很快过期。
    _expire_lease(repo, claimed.job_id)
    return claimed.job_id


def simulate_crash_and_recover(db: Database, lease_seconds: int = 5) -> dict:
    """完整崩溃 → 恢复 → 重跑流程，返回恢复后的终态 job。

    Returns
    -------
    dict with keys: job_id, final_status, result, requeued.
    """
    repo = JobRepository(db, lease_seconds=lease_seconds)
    # 崩溃前：Worker-1 认领并标记 RUNNING 后崩溃
    job_id = crash_after_running(repo, "worker-1")

    # 新进程（Worker-2）启动：同一数据库，调用 recover()
    repo2 = JobRepository(db, lease_seconds=lease_seconds)
    stats = repo2.recover()
    assert stats["requeued"] == 1, f"应有 1 个 job 被重新入队: {stats}"

    # Worker-2 重新认领并成功执行
    reclaimed = repo2.claim("worker-2")
    assert reclaimed is not None, "恢复后应可重新认领"
    assert reclaimed.job_id == job_id
    assert repo2.succeed(reclaimed.job_id, "worker-2", {"recovered": True})

    final = repo2.get(job_id)
    return {
        "job_id": job_id,
        "final_status": final.status,
        "result": final.result,
        "requeued": stats["requeued"],
    }


def _expire_lease(repo: JobRepository, job_id: str) -> None:
    """把 job 租约置为过去（模拟崩溃后不再续约）。"""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
    with repo.db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job_id))


__all__ = ["crash_after_running", "simulate_crash_and_recover"]
