"""Control API v1 稳定 DTO（API-001）。

合同约束（TARGET_ARCHITECTURE_V3 §7.1）：
- API 只返回这些结构化 DTO 的 JSON，不返回 Markdown / DataFrame；
- 字段名与类型是合同的一部分：删除或改名属于 breaking change，
  由合同测试（`tests/test_control_api_contract.py` + `openapi_v1.json`）守护。

DTO 保持 `to_dict()` 单一序列化入口，路由层统一包装为
`{"status": ..., "data": ...}` 或 `{"status": ..., "error": {...}}`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class ErrorDTO:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


@dataclass(frozen=True, slots=True)
class HealthDTO:
    """GET /api/v1/data/health"""

    ok: bool
    freshness_days: int | None
    message: str
    snapshot_id: str | None = None
    rule_book_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "freshness_days": self.freshness_days,
            "message": self.message,
            "snapshot_id": self.snapshot_id,
            "rule_book_version": self.rule_book_version,
        }


@dataclass(frozen=True, slots=True)
class JobDTO:
    """POST /api/v1/jobs 与 GET /api/v1/jobs/{job_id}"""

    job_id: str
    job_type: str
    status: str
    idempotency_key: str | None
    attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class JobEventsDTO:
    """GET /api/v1/jobs/{job_id}/events"""

    job_id: str
    status: str
    line_count: int
    lines: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "line_count": self.line_count,
            "lines": list(self.lines),
        }


@dataclass(frozen=True, slots=True)
class ArtifactRunDTO:
    """GET /api/v1/artifacts/{run_id}"""

    run_id: str
    task: str
    created_at: str
    fingerprint: str | None
    files: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "files": list(self.files),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TradePlanDTO:
    """POST /api/v1/trade-plans/{plan_id}/approve"""

    plan_id: str
    status: str
    account_id: str | None
    signal_date: str | None
    intended_trade_date: str | None
    order_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "account_id": self.account_id,
            "signal_date": self.signal_date,
            "intended_trade_date": self.intended_trade_date,
            "order_count": self.order_count,
        }


@dataclass(frozen=True, slots=True)
class PositionsDTO:
    """GET /api/v1/accounts/{account_id}/positions

    由 OMS 成交账本推导的持仓查询模型（只读派生视图，不是账户权威源）。
    """

    account_id: str
    positions: dict[str, int]
    derived_from: str = "oms_fills"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "positions": dict(self.positions),
            "derived_from": self.derived_from,
        }


#: 合同内全部 DTO（供 OpenAPI 生成与合同测试枚举）
CONTRACT_DTOS: tuple[type, ...] = (
    ErrorDTO,
    HealthDTO,
    JobDTO,
    JobEventsDTO,
    ArtifactRunDTO,
    TradePlanDTO,
    PositionsDTO,
)

__all__ = [
    "API_VERSION",
    "CONTRACT_DTOS",
    "ArtifactRunDTO",
    "ErrorDTO",
    "HealthDTO",
    "JobDTO",
    "JobEventsDTO",
    "PositionsDTO",
    "TradePlanDTO",
]
