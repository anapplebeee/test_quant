"""JOB-001 验收：持久化任务接入 task_api（重启恢复、幂等、终态镜像）。

注意：`submit()` 会立即调度并启动子进程。本文件用 no_dispatch 夹具
隔离持久化语义，不真正启动脚本；仅"拒绝启动"一例调用真实 _start_task。
"""
from __future__ import annotations

import pytest

import api.task_api as task_api
from api.persistent_task_backend import PersistentTaskBackend
from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository


@pytest.fixture()
def backend(tmp_path):
    repo = JobRepository(Database(tmp_path / "jobs.db"), lease_seconds=300)
    return PersistentTaskBackend(repo)


@pytest.fixture()
def echo_task_registered(monkeypatch):
    monkeypatch.setitem(task_api.TASKS, "echo_test", {
        "name": "回声任务", "script": "scripts/_echo.py", "args": [],
        "resource": "compute", "timeout": 60,
    })
    monkeypatch.setitem(task_api.ALLOWED_ARGS, "echo_test", {})


@pytest.fixture()
def no_dispatch(monkeypatch):
    """禁止真实启动子进程：只记录被调度的任务，保持内存状态为排队中。"""
    started: list[str] = []

    def fake_start(self, task):
        started.append(task.task_id)

    monkeypatch.setattr(task_api.TaskQueue, "_start_task", fake_start)
    monkeypatch.setattr(task_api.TaskQueue, "_dispatch_loop", lambda self: None)
    return started


def _expire_lease(repo: JobRepository, job_id: str) -> None:
    with repo.db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
            (job_id,),
        )


def _claim_as_task_api(repo: JobRepository, job_id: str) -> None:
    """模拟 _start_task 的认领（真实路径在启动子进程前执行）。"""
    assert repo.claim_job(job_id, "task_api") is not None
    assert repo.mark_running(job_id, "task_api")


def test_submit_creates_persistent_job(backend, echo_task_registered, no_dispatch):
    q = task_api.TaskQueue(backend=backend)
    ok, msg, instance_id = q.submit("echo_test")
    assert ok, msg
    task = q.tasks[instance_id]
    assert task.job_id is not None
    job = backend.repo.get(task.job_id)
    assert job is not None and job.status == "QUEUED"
    assert job.payload["script"] == "scripts/_echo.py"
    assert instance_id in no_dispatch  # 调度器确实尝试启动了它


def test_duplicate_submission_within_process_rejected(backend, echo_task_registered, no_dispatch):
    q = task_api.TaskQueue(backend=backend)
    ok, _, _ = q.submit("echo_test")
    assert ok
    ok, msg, _ = q.submit("echo_test")
    assert not ok
    assert "已在队列中" in msg


def test_duplicate_submission_across_restart_rejected(backend, echo_task_registered, no_dispatch):
    """重启后重复请求不能产生重复任务（Phase A 验收）。"""
    q1 = task_api.TaskQueue(backend=backend)
    ok, _, _ = q1.submit("echo_test")
    assert ok

    q2 = task_api.TaskQueue(backend=backend)  # 模拟进程重启：内存队列全新
    ok, msg, _ = q2.submit("echo_test")
    assert not ok
    assert "持久化任务已存在" in msg


def test_restart_recovery_hydrates_leftover_jobs(backend, echo_task_registered, no_dispatch):
    """应用重启后任务可恢复：遗留 QUEUED job 被水合回内存队列。"""
    q1 = task_api.TaskQueue(backend=backend)
    ok, _, instance_id = q1.submit("echo_test")
    assert ok
    job_id = q1.tasks[instance_id].job_id

    q2 = task_api.TaskQueue(backend=backend)
    q2._recover_persisted()

    restored = [t for t in q2.tasks.values() if t.job_id == job_id]
    assert len(restored) == 1
    assert restored[0].status == task_api.TaskStatus.PENDING
    assert restored[0].script == "scripts/_echo.py"


def test_restart_recovery_requeues_stale_running(backend, echo_task_registered, no_dispatch):
    """崩溃时处于 RUNNING 且租约过期的 job：recovery 重入队而非丢失。"""
    q1 = task_api.TaskQueue(backend=backend)
    ok, _, instance_id = q1.submit("echo_test")
    assert ok
    job_id = q1.tasks[instance_id].job_id

    repo = backend.repo
    _claim_as_task_api(repo, job_id)  # 模拟任务已在运行中崩溃
    _expire_lease(repo, job_id)

    q2 = task_api.TaskQueue(backend=backend)
    q2._recover_persisted()

    job = repo.get(job_id)
    assert job.status == "QUEUED"  # attempts(1) < max_attempts(3) → 重试
    assert any(t.job_id == job_id for t in q2.tasks.values())


def test_recovery_fails_job_over_max_attempts(backend):
    repo = backend.repo
    job = repo.create("echo_test", payload={"script": "s.py", "args": [], "resource": "compute"})
    repo.claim_job(job.job_id, "w")
    repo.mark_running(job.job_id, "w")
    with repo.db.connect() as conn:
        conn.execute("UPDATE jobs SET attempts = max_attempts WHERE job_id = ?", (job.job_id,))
    _expire_lease(repo, job.job_id)

    q2 = task_api.TaskQueue(backend=backend)
    q2._recover_persisted()

    assert repo.get(job.job_id).status == "FAILED"
    assert not any(t.job_id == job.job_id for t in q2.tasks.values())


def test_finalize_mirrors_terminal_states(backend, echo_task_registered, no_dispatch):
    q = task_api.TaskQueue(backend=backend)
    ok, _, instance_id = q.submit("echo_test")
    assert ok
    task = q.tasks[instance_id]
    _claim_as_task_api(backend.repo, task.job_id)

    task.status = task_api.TaskStatus.COMPLETED
    task.returncode = 0
    q._finalize_persistent(task)
    assert backend.repo.get(task.job_id).status == "SUCCEEDED"

    # 上一个已终态，相同参数允许再次提交
    ok, _, second = q.submit("echo_test")
    assert ok
    task2 = q.tasks[second]
    assert task2.job_id != task.job_id
    _claim_as_task_api(backend.repo, task2.job_id)
    task2.status = task_api.TaskStatus.FAILED
    task2.returncode = 2
    q._finalize_persistent(task2)
    job2 = backend.repo.get(task2.job_id)
    assert job2.status == "FAILED" and "returncode=2" in (job2.error or "")


def test_finalize_mirrors_cancel(backend, echo_task_registered, no_dispatch):
    q = task_api.TaskQueue(backend=backend)
    ok, _, instance_id = q.submit("echo_test")
    assert ok
    task = q.tasks[instance_id]
    ok, msg = q.cancel(instance_id)  # 排队中取消
    assert ok, msg
    assert backend.repo.get(task.job_id).status == "CANCELLED"


def test_start_task_refuses_when_persistent_state_terminal(
    backend, echo_task_registered, monkeypatch
):
    """持久化记录已终态时不得启动子进程（防止重复执行）。"""
    started: list[str] = []
    original = task_api.TaskQueue._start_task

    def fake_start(self, task):
        started.append(task.task_id)

    monkeypatch.setattr(task_api.TaskQueue, "_start_task", fake_start)
    monkeypatch.setattr(task_api.TaskQueue, "_dispatch_loop", lambda self: None)
    q = task_api.TaskQueue(backend=backend)
    ok, _, instance_id = q.submit("echo_test")
    assert ok
    task = q.tasks[instance_id]

    repo = backend.repo
    repo.claim_job(task.job_id, "someone")
    repo.mark_running(task.job_id, "someone")
    repo.succeed(task.job_id, "someone", result={})  # 被其他路径置为终态

    monkeypatch.setattr(task_api.TaskQueue, "_start_task", original)
    q._start_task(task)

    assert task.status == task_api.TaskStatus.CANCELLED
    assert task.process is None


def test_in_memory_mode_works_without_repo(echo_task_registered, no_dispatch):
    q = task_api.TaskQueue(persistent=False)
    ok, msg, instance_id = q.submit("echo_test")
    assert ok, msg
    assert q.tasks[instance_id].job_id is None
    assert q.backend.recover() == []
