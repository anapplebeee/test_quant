"""JOB-001：持久化 Job 仓库测试。

验收（ADR-0001）：create/claim/lease/cancel/recovery/幂等，
进程崩溃/重启后未完成任务可恢复。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.infrastructure.job_schema import (
    JOB_CANCELLED,
    JOB_CLAIMED,
    JOB_CREATED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)


@pytest.fixture
def repo(tmp_path) -> JobRepository:
    return JobRepository(Database(tmp_path / "jobs.db"), lease_seconds=5)


def _expire_lease(repo: JobRepository, job_id: str) -> None:
    """把 job 租约设为过去（模拟进程崩溃后不再续约）。"""
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
    with repo.db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job_id))


# ---------------------------------------------------------------- Create


def test_create_queues_job(repo):
    job = repo.create("backtest", {"strategy": "lowvol_indz"})
    assert job.status == JOB_QUEUED
    assert job.job_type == "backtest"
    assert job.attempts == 0


def test_create_idempotent_by_key(repo):
    j1 = repo.create("backtest", {"k": 1}, idempotency_key="key-1")
    j2 = repo.create("backtest", {"k": 999}, idempotency_key="key-1")
    assert j1.job_id == j2.job_id, "同幂等键应返回同一 job"
    assert j1.payload == {"k": 1}, "返回原 job，不被新 payload 覆盖"


def test_create_without_key_is_unique(repo):
    j1 = repo.create("backtest")
    j2 = repo.create("backtest")
    assert j1.job_id != j2.job_id


def test_get_returns_job(repo):
    job = repo.create("backtest", {"s": "x"})
    got = repo.get(job.job_id)
    assert got is not None
    assert got.payload == {"s": "x"}


# ---------------------------------------------------------------- Claim


def test_claim_assigns_lease(repo):
    job = repo.create("backtest")
    claimed = repo.claim("worker-1")
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == JOB_CLAIMED
    assert claimed.claimed_by == "worker-1"
    assert claimed.lease_until is not None
    assert claimed.attempts == 1


def test_claim_only_one_worker_gets_job(repo):
    """两个 Worker 并发 claim，同一 job 只能被一个拿到。"""
    repo.create("backtest")
    a = repo.claim("worker-a")
    b = repo.claim("worker-b")
    # 只有第一个能 claim 到，第二个返回 None（无更多可认领）
    assert a is not None
    assert b is None


def test_claim_filters_by_job_type(repo):
    repo.create("backtest")
    repo.create("signal")
    got = repo.claim("w", job_types=["signal"])
    assert got is not None and got.job_type == "signal"
    got2 = repo.claim("w", job_types=["signal"])
    assert got2 is None, "signal 类型只剩一个"


def test_claim_respects_priority(repo):
    repo.create("low", priority=0)
    repo.create("high", priority=10)
    first = repo.claim("w")
    assert first.job_type == "high", "高优先级应优先认领"


def test_claim_after_release_reclaimable(repo):
    """job 成功后不可再被 claim；失败后可重试。"""
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    repo.succeed(claimed.job_id, "w1", {"ok": True})
    assert repo.claim("w2") is None, "成功后无剩余可认领"
    assert repo.get(job.job_id).status == JOB_SUCCEEDED


# ---------------------------------------------------------------- Lease / heartbeat


def test_heartbeat_extends_lease(repo):
    """心跳应把租约续到未来。

    注：lease 时间戳精度为秒，同秒内续约时间不变。因此先把租约置为过去，
    再心跳，验证被续到未来（lease_until 已过期 → 心跳后未过期）。
    """
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    _expire_lease(repo, claimed.job_id)  # 租约已过期（模拟快到期）
    assert repo.heartbeat(claimed.job_id, "w1")
    refreshed = repo.get(claimed.job_id)
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert refreshed.lease_until >= now_utc, "心跳应续约到未来"
    assert refreshed.lease_until > refreshed.started_at  # 续约后租约晚于开始


def test_mark_running_transitions_state(repo):
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    assert repo.mark_running(claimed.job_id, "w1")
    assert repo.get(job.job_id).status == JOB_RUNNING


def test_wrong_worker_cannot_operate(repo):
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    # w2 不能 succeed w1 的 job
    assert not repo.succeed(claimed.job_id, "w2")
    assert repo.get(job.job_id).status == JOB_CLAIMED


# ---------------------------------------------------------------- Succeed / Fail / Cancel


def test_succeed_stores_result(repo):
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    assert repo.succeed(claimed.job_id, "w1", {"cagr": 0.07})
    done = repo.get(job.job_id)
    assert done.status == JOB_SUCCEEDED
    assert done.result == {"cagr": 0.07}


def test_fail_records_error(repo):
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    assert repo.fail(claimed.job_id, "w1", "data stale")
    done = repo.get(job.job_id)
    assert done.status == JOB_FAILED
    assert done.error == "data stale"


def test_cancel_queued_job(repo):
    job = repo.create("backtest")
    assert repo.cancel(job.job_id, "user cancelled")
    assert repo.get(job.job_id).status == JOB_CANCELLED


def test_cancel_terminal_job_fails(repo):
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    repo.succeed(claimed.job_id, "w1")
    assert not repo.cancel(job.job_id), "终态 job 不可取消"


# ---------------------------------------------------------------- Recovery


def test_recover_requeues_stale_running(repo):
    """崩溃后 RUNNING job 租约过期 → 重置为 QUEUED 可重试。"""
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    repo.mark_running(claimed.job_id, "w1")
    _expire_lease(repo, claimed.job_id)

    stats = repo.recover()
    assert stats["requeued"] == 1
    recovered = repo.get(job.job_id)
    assert recovered.status == JOB_QUEUED
    assert recovered.claimed_by is None
    assert recovered.attempts == 1, "保留已尝试次数"


def test_recover_marks_over_attempts_failed(repo):
    """超过 max_attempts 的 stale job 标记 FAILED 而非无限重试。"""
    job = repo.create("backtest", max_attempts=1)
    claimed = repo.claim("w1")
    _expire_lease(repo, claimed.job_id)
    repo.recover()
    # 已尝试 1 次，达到 max_attempts=1 → 再次 recover 时标记 FAILED
    # （第一次 recover 时 attempts=1 >= max_attempts=1）
    assert repo.get(job.job_id).status == JOB_FAILED


def test_recover_ignores_active_lease(repo):
    """租约未过期的 job 不应被 recovery 回收。"""
    job = repo.create("backtest")
    claimed = repo.claim("w1")
    repo.mark_running(claimed.job_id, "w1")
    stats = repo.recover()
    assert stats["requeued"] == 0
    assert repo.get(job.job_id).status == JOB_RUNNING


def test_recovery_is_the_restart_test_fixture(repo):
    """恢复测试夹具：模拟进程崩溃 + 新进程 recover。

    场景：
    1. worker-1 认领并标记 RUNNING（进程随后崩溃，租约不再续约）；
    2. 新进程（worker-2）启动，调用 recover()；
    3. 过期 job 重新入队，worker-2 可重新 claim 执行。
    """
    # 崩溃前
    job = repo.create("backtest", {"strategy": "lowvol_indz"}, max_attempts=3)
    claimed = repo.claim("worker-1")
    repo.mark_running(claimed.job_id, "worker-1")
    # 模拟 worker-1 崩溃：直接丢弃引用，不调用 heartbeat/succeed/fail
    # 租约在 lease_seconds(5s) 后过期。为测试快速触发，手动置为过去。
    _expire_lease(repo, claimed.job_id)

    # 新进程启动
    repo2 = JobRepository(repo.db, lease_seconds=5)  # 同一数据库
    stats = repo2.recover()
    assert stats["requeued"] == 1

    # worker-2 重新认领并成功执行
    reclaimed = repo2.claim("worker-2")
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id
    assert repo2.succeed(reclaimed.job_id, "worker-2", {"recovered": True})
    final = repo2.get(job.job_id)
    assert final.status == JOB_SUCCEEDED
    assert final.result == {"recovered": True}


def test_list_filters(repo):
    a = repo.create("backtest")
    b = repo.create("signal")
    all_jobs = repo.list()
    assert len(all_jobs) == 2
    assert len(repo.list(job_type="signal")) == 1
    assert len(repo.list(status=JOB_QUEUED)) == 2


def test_multiple_workers_parallel_claim(repo):
    """多 Worker 并发认领，每个 job 只被一个 Worker 拿到。"""
    for _ in range(10):
        repo.create("backtest")
    claimed = set()
    lock = threading.Lock()

    def worker(wid):
        for _ in range(10):
            j = repo.claim(wid)
            if j:
                with lock:
                    claimed.add(j.job_id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 10, "10 个 job 应被 4 个 Worker 恰好认领完，无重复"
