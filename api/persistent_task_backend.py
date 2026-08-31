"""task_api 与持久化 Job 仓库之间的适配层（JOB-001 接线）。

职责边界
--------
- `api/task_api.py` 保持现有前端合同（submit/get_output/cancel/状态摘要）；
- 本模块负责把任务提交/状态变化镜像到 `JobRepository`（SQLite），
  并在进程启动时恢复上次未完成的持久化任务。

持久化语义（Phase A 验收：重启可恢复、重复请求不产生重复任务）
--------------------------------------------------------------
- 提交 = 先落库（QUEUED）再进入内存调度；落库失败不阻塞内存提交，
  持久化降级为告警（日频研究工具可用性优先，状态可恢复性尽力而为）；
- 幂等 = 同 (job_type + 规范化参数) 已有非终态 job 时，返回该 job
  而不是新建；终态后的相同参数允许重新提交（与现有"完成后可重跑"语义一致）；
- 恢复 = 启动时把上次进程遗留的 CLAIMED/RUNNING 且租约过期的 job
  标记为重入队，再水合为内存任务继续执行。子进程本身已随父进程死亡，
  重跑是恢复语义（同 JOB-001 recovery），不是重复执行。
"""
from __future__ import annotations

import hashlib
import json

from loguru import logger

from quart.infrastructure.job import Job, JobRepository
from quart.infrastructure.job_schema import TERMINAL

_WORKER_ID = "task_api"


def _idempotency_key(family: str, args: list[str]) -> str:
    canonical = json.dumps({"family": family, "args": args}, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class PersistentTaskBackend:
    """TaskQueue 的持久化镜像。仓储为 None 时整体退化为纯内存（测试/无库环境）。"""

    def __init__(self, repo: JobRepository | None = None):
        self.repo = repo

    # ---------------- 提交 ----------------

    def submit(
        self, family: str, args: list[str], script: str, resource: str
    ) -> tuple[bool, str, str | None]:
        """落库一个新任务。

        Returns
        -------
        (是否继续创建内存任务, 消息, job_id)。
        幂等命中时返回既有 job 的 id 与提示，调用方应拒绝重复提交。
        """
        if self.repo is None:
            return True, "", None
        key = _idempotency_key(family, args)
        create_key: str | None = key
        try:
            existing = self.repo.get_by_idempotency_key(key)
            if existing is not None:
                if existing.status not in TERMINAL:
                    return False, f"持久化任务已存在（{existing.status}）", existing.job_id
                # 已终态：允许重新执行。丢弃幂等键，避免 create 直接返回旧的终态记录。
                create_key = None
            job = self.repo.create(
                job_type=family,
                payload={"script": script, "args": args, "resource": resource},
                idempotency_key=create_key,
            )
            return True, "", job.job_id
        except Exception as exc:  # 持久化故障不阻塞内存提交
            logger.warning("persistent job create failed: {}", exc)
            return True, "", None

    # ---------------- 状态镜像 ----------------

    def claim_and_run(self, job_id: str | None) -> bool:
        """认领指定 job 并置为 RUNNING；状态不允许（已被认领/终态）返回 False。"""
        if self.repo is None or not job_id:
            return True
        try:
            if self.repo.claim_job(job_id, _WORKER_ID) is None:
                return False
            return self.repo.mark_running(job_id, _WORKER_ID)
        except Exception as exc:  # pragma: no cover - 镜像失败不影响执行
            logger.warning("persistent claim_and_run failed: {}", exc)
            return True

    def heartbeat(self, job_id: str | None) -> None:
        if self.repo is None or not job_id:
            return
        try:
            self.repo.heartbeat(job_id, _WORKER_ID)
        except Exception as exc:  # pragma: no cover
            logger.warning("persistent heartbeat failed: {}", exc)

    def finish(self, job_id: str | None, returncode: int, error: str | None = None) -> None:
        if self.repo is None or not job_id:
            return
        try:
            if returncode == 0:
                self.repo.succeed(job_id, _WORKER_ID, result={"returncode": returncode})
            else:
                self.repo.fail(job_id, _WORKER_ID, error or f"returncode={returncode}")
        except Exception as exc:  # pragma: no cover
            logger.warning("persistent finish failed: {}", exc)

    def cancel(self, job_id: str | None, reason: str | None = None) -> None:
        if self.repo is None or not job_id:
            return
        try:
            self.repo.cancel(job_id, reason)
        except Exception as exc:  # pragma: no cover
            logger.warning("persistent cancel failed: {}", exc)

    # ---------------- 启动恢复 ----------------

    def recover(self) -> list[Job]:
        """进程启动时恢复上次遗留任务，返回需要重新调度的 job 列表。

        上次进程内运行中（CLAIMED）或刚被水合（QUEUED，lease 仍有效）的
        job 会被重入队；子进程已随父进程退出，重跑即恢复。
        """
        if self.repo is None:
            return []
        try:
            stats = self.repo.recover()
            if stats["requeued"] or stats["failed"]:
                logger.info("job recovery: {}", stats)
            jobs: list[Job] = []
            for job in self.repo.list(status="QUEUED", limit=100):
                payload = job.payload or {}
                if payload.get("script") and payload.get("resource"):
                    jobs.append(job)
                else:
                    # 无脚本信息的 QUEUED job（外部直接入队）无法被 task_api 执行
                    self.repo.fail(job.job_id, _WORKER_ID, "recoverable payload missing script")
            return jobs
        except Exception as exc:
            logger.warning("job recovery failed: {}", exc)
            return []


__all__ = ["PersistentTaskBackend"]
