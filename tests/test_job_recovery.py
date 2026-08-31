"""JOB 恢复测试（复用 job_recovery_fixtures）。

协调文档批次 1 出口门槛："Job 可重启恢复"。
"""
from __future__ import annotations

import pytest

from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.infrastructure.job_schema import JOB_RUNNING, JOB_SUCCEEDED
from tests.job_recovery_fixtures import crash_after_running, simulate_crash_and_recover


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "recovery.db")


def test_crash_after_running_leaves_running(db):
    """崩溃前 job 处于 RUNNING（租约已过期）。"""
    repo = JobRepository(db, lease_seconds=5)
    job_id = crash_after_running(repo, "worker-1")
    job = repo.get(job_id)
    assert job.status == JOB_RUNNING
    assert job.claimed_by == "worker-1"


def test_simulate_crash_and_recover(db):
    """完整恢复流程：崩溃 → recover → 重跑 → 成功。"""
    result = simulate_crash_and_recover(db, lease_seconds=5)
    assert result["requeued"] == 1
    assert result["final_status"] == JOB_SUCCEEDED
    assert result["result"] == {"recovered": True}


def test_recovered_job_retains_attempt_count(db):
    """恢复后重新执行的 job 保留崩溃前的尝试次数。"""
    repo = JobRepository(db, lease_seconds=5)
    job_id = crash_after_running(repo, "worker-1")
    # 崩溃前已 attempts=1
    assert repo.get(job_id).attempts == 1
    repo.recover()
    reclaimed = repo.claim("worker-2")
    # 重新 claim 后 attempts=2（1 次崩溃 + 1 次重试）
    assert reclaimed.attempts == 2


def test_recover_after_partial_fill_idempotent(db):
    """多次 recover 应幂等：只有一次有效回收。"""
    repo = JobRepository(db, lease_seconds=5)
    job_id = crash_after_running(repo)
    s1 = repo.recover()
    s2 = repo.recover()
    assert s1["requeued"] == 1
    assert s2["requeued"] == 0, "第二次 recover 不应重复回收"
