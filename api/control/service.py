"""Control API v1 application service（API-001）。

路由层（`api/control/router.py`）只做校验与映射，业务逻辑集中在这里。
所有依赖都可注入（测试用临时库），缺省落到平台现有单例：

- 任务：`api.task_api.task_queue`（内存 + JOB-001 持久化镜像）
- 制品：`api.artifacts_api.get_run`
- 交易计划：`quart.manual_trading.TradingRepository`
- 持仓：`quart.oms.OrderRepository`（成交账本派生的查询模型）
- 健康：`BarStore` 数据新鲜度 + 最新数据快照 + 规则书版本

报单/撤单/对账端点是合同占位：返回 503，等待 `BROKER-001` 与
对账流程落地后接线。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from api.control.dto import (
    ArtifactRunDTO,
    HealthDTO,
    JobDTO,
    JobEventsDTO,
    PositionsDTO,
    TradePlanDTO,
)
from api.control.errors import ApiError, ApiErrorCode

BROKER_PENDING = "等待 BROKER-001（PaperBroker 持久化与恢复）落地后开放"
RECON_PENDING = "等待对账流程（BROKER-001 收盘对账）落地后开放"


class ControlServiceV1:
    """Control API v1 的 application service。"""

    def __init__(
        self,
        *,
        task_queue: object | None = None,
        job_repo: object | None = None,
        artifacts_getter: Callable[[str], dict | None] | None = None,
        trading_repo: object | None = None,
        order_repo: object | None = None,
        freshness_probe: Callable[[], int | None] | None = None,
        snapshot_probe: Callable[[], str | None] | None = None,
    ):
        self._task_queue = task_queue
        self._job_repo = job_repo
        self._artifacts_getter = artifacts_getter
        self._trading_repo = trading_repo
        self._order_repo = order_repo
        self._freshness_probe = freshness_probe
        self._snapshot_probe = snapshot_probe

    # ---------------- 依赖惰性解析（缺省用平台单例） ----------------

    @property
    def task_queue(self):
        if self._task_queue is None:
            from api.task_api import task_queue

            self._task_queue = task_queue
        return self._task_queue

    @property
    def job_repo(self):
        if self._job_repo is None:
            from quart.infrastructure.job import JobRepository

            self._job_repo = JobRepository()
        return self._job_repo

    @property
    def artifacts_getter(self) -> Callable[[str], dict | None]:
        if self._artifacts_getter is None:
            from api.artifacts_api import get_run

            self._artifacts_getter = get_run
        return self._artifacts_getter

    @property
    def trading_repo(self):
        if self._trading_repo is None:
            from pathlib import Path

            from quart.config import PROJECT_ROOT, load_config
            from quart.manual_trading import TradingRepository

            cfg = load_config()
            manual_cfg = cfg.get("manual_trading", {})
            db_path = Path(manual_cfg.get("database", PROJECT_ROOT / "state" / "trading.db"))
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            repo = TradingRepository(db_path)
            repo.initialize_schema()
            self._trading_repo = repo
        return self._trading_repo

    # ---------------- GET /api/v1/data/health ----------------

    def data_health(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> HealthDTO:
        freshness = (
            self._freshness_probe() if self._freshness_probe is not None
            else self._default_freshness()
        )
        snapshot_id = (
            self._snapshot_probe() if self._snapshot_probe is not None
            else self._default_snapshot_id()
        )
        rule_book_version = self._default_rule_book_version()

        if freshness is None:
            ok, message = False, "本地数据为空，请先运行数据刷新任务"
        elif freshness > 5:
            ok, message = False, f"数据已过期 {freshness} 天，不可用于正式信号"
        elif freshness > 2:
            ok, message = True, f"数据落后 {freshness} 天，建议尽快刷新"
        else:
            ok, message = True, "数据新鲜"
        return HealthDTO(
            ok=ok,
            freshness_days=freshness,
            message=message,
            snapshot_id=snapshot_id,
            rule_book_version=rule_book_version,
        )

    @staticmethod
    def _default_freshness() -> int | None:
        try:
            from quart.data.store import BarStore

            return BarStore().freshness_days()
        except Exception as exc:
            logger.warning("control api: freshness probe failed: {}", exc)
            return None

    @staticmethod
    def _default_snapshot_id() -> str | None:
        try:
            from quart.data.snapshot import list_snapshots, load_manifest

            ids = list_snapshots("daily")
            if not ids:
                return None
            return load_manifest("daily", ids[-1]).snapshot_id
        except Exception as exc:
            logger.warning("control api: snapshot probe failed: {}", exc)
            return None

    @staticmethod
    def _default_rule_book_version() -> str | None:
        try:
            from quart.market_rules.rule_book import load_rule_book_version

            return load_rule_book_version()
        except Exception as exc:
            logger.warning("control api: rule book version failed: {}", exc)
            return None

    # ---------------- jobs ----------------

    def submit_job(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> tuple[JobDTO, int]:
        job_type = str(body.get("job_type") or "").strip()
        if not job_type:
            raise ApiError(ApiErrorCode.VALIDATION_ERROR, "job_type 必填")
        args = body.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ApiError(ApiErrorCode.VALIDATION_ERROR, "args 必须是字符串数组")

        # 幂等重试：同键已有记录直接返回原 job（200），不再走提交流程
        if idempotency_key:
            existing = self.job_repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._job_dto(existing), 200

        ok, msg, instance_id = self.task_queue.submit(
            job_type, extra_args=args, idempotency_key=idempotency_key
        )
        if not ok:
            code = ApiErrorCode.VALIDATION_ERROR
            if "已在队列中" in msg or "已存在" in msg:
                code = ApiErrorCode.CONFLICT
            raise ApiError(code, msg)

        job = self.job_repo.get_by_idempotency_key(idempotency_key) if idempotency_key else None
        if job is None and instance_id:
            job = self.job_repo.get(instance_id)
        if job is not None:
            return self._job_dto(job), 201
        # 纯内存模式（无持久化库）退化为内存任务视图
        task = self.task_queue.tasks.get(instance_id)
        if task is None:
            raise ApiError(ApiErrorCode.INTERNAL, "任务已提交但无法定位记录")
        return self._task_dto(task), 201

    def get_job(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> JobDTO:
        job_id = params["job_id"]
        job = self.job_repo.get(job_id)
        if job is not None:
            return self._job_dto(job)
        task = self.task_queue.tasks.get(job_id)
        if task is not None:
            return self._task_dto(task)
        raise ApiError(ApiErrorCode.NOT_FOUND, f"任务不存在: {job_id}")

    def job_events(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> JobEventsDTO:
        job_id = params["job_id"]
        tail = int(body.get("tail", 200)) if isinstance(body.get("tail"), int) else 200
        task = self.task_queue.tasks.get(job_id)
        if task is not None:
            lines = list(task.output_lines[-tail:])
            return JobEventsDTO(
                job_id=job_id,
                status=str(task.status.value),
                line_count=len(task.output_lines),
                lines=lines,
            )
        job = self.job_repo.get(job_id)
        if job is not None:
            return JobEventsDTO(job_id=job_id, status=job.status, line_count=0, lines=[])
        raise ApiError(ApiErrorCode.NOT_FOUND, f"任务不存在: {job_id}")

    @staticmethod
    def _job_dto(job) -> JobDTO:
        return JobDTO(
            job_id=job.job_id,
            job_type=job.job_type,
            status=job.status,
            idempotency_key=job.idempotency_key,
            attempts=job.attempts,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
        )

    @staticmethod
    def _task_dto(task) -> JobDTO:
        return JobDTO(
            job_id=task.task_id,
            job_type=task.family,
            status=str(task.status.value),
            idempotency_key=task.job_id,
            attempts=0,
            created_at=task.created_at.isoformat(),
            started_at=task.started_at.isoformat() if task.started_at else None,
            finished_at=task.ended_at.isoformat() if task.ended_at else None,
            error=None,
        )

    # ---------------- artifacts ----------------

    def get_artifact(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> ArtifactRunDTO:
        run_id = params["run_id"]
        run = self.artifacts_getter(run_id)
        if run is None:
            raise ApiError(ApiErrorCode.NOT_FOUND, f"制品运行不存在: {run_id}")
        artifacts = run.get("artifacts") or []
        return ArtifactRunDTO(
            run_id=str(run.get("run_id") or run_id),
            task=str(run.get("task") or ""),
            created_at=str(run.get("created_at") or ""),
            fingerprint=run.get("fingerprint"),
            files=[str(a.get("name")) for a in artifacts],
            metadata={
                k: run.get(k)
                for k in ("status", "params", "data_version", "code", "metrics", "error")
                if run.get(k) is not None
            },
        )

    # ---------------- trade plans ----------------

    def approve_trade_plan(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> TradePlanDTO:
        plan_id = params["plan_id"]
        repo = self.trading_repo
        detail = repo.plan_detail(plan_id)
        if detail is None:
            raise ApiError(ApiErrorCode.NOT_FOUND, f"交易计划不存在: {plan_id}")
        repo.approve_plan(plan_id)
        detail = repo.plan_detail(plan_id) or detail
        orders = detail.get("orders") or []
        return TradePlanDTO(
            plan_id=str(detail.get("plan_id") or plan_id),
            status=str(detail.get("status") or ""),
            account_id=detail.get("account_id"),
            signal_date=detail.get("signal_date"),
            intended_trade_date=detail.get("intended_trade_date"),
            order_count=len(orders),
        )

    # ---------------- positions（OMS-001 接线） ----------------

    def get_positions(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> PositionsDTO:
        account_id = params["account_id"]
        positions = self.order_repo.positions_from_fills(account_id)
        return PositionsDTO(account_id=account_id, positions=positions)

    @property
    def order_repo(self):
        if self._order_repo is None:
            from quart.oms import OrderRepository

            self._order_repo = OrderRepository()
        return self._order_repo

    # ---------------- 合同占位：等待 BROKER-001 / 对账 ----------------

    def create_order(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> None:
        raise ApiError(ApiErrorCode.SERVICE_UNAVAILABLE, BROKER_PENDING)

    def cancel_order(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> None:
        raise ApiError(ApiErrorCode.SERVICE_UNAVAILABLE, BROKER_PENDING)

    def create_reconciliation(
        self, params: dict[str, str], body: dict[str, Any], idempotency_key: str | None
    ) -> None:
        raise ApiError(ApiErrorCode.SERVICE_UNAVAILABLE, RECON_PENDING)


__all__ = ["BROKER_PENDING", "RECON_PENDING", "ControlServiceV1"]
