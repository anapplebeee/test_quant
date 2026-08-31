"""Control API v1 合同测试（API-001，TARGET_ARCHITECTURE_V3 §7.1）。

守护内容：
- 路由表（10 个端点）与错误语义（400/404/405/409/500/503）；
- mutation 请求必须携带 Idempotency-Key，重放返回原结果；
- DTO 字段名是合同的一部分（改名/删除 → 测试失败）;
- 冻结的 OpenAPI 规范与代码生成结果一致（breaking-change 检查）；
- OMS 占位端点显式 503（等待 OMS-001）。
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from api.control.dto import CONTRACT_DTOS
from api.control.errors import ApiError, ApiErrorCode
from api.control.openapi import OPENAPI_PATH, generate_openapi
from api.control.router import ROUTES, ControlRouter
from api.control.service import ControlServiceV1

# ---------------- 路由合同 ----------------

EXPECTED_ROUTES = {
    ("GET", "/api/v1/data/health"),
    ("POST", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/jobs/{job_id}/events"),
    ("GET", "/api/v1/artifacts/{run_id}"),
    ("POST", "/api/v1/trade-plans/{plan_id}/approve"),
    ("POST", "/api/v1/orders"),
    ("POST", "/api/v1/orders/{order_id}/cancel"),
    ("GET", "/api/v1/accounts/{account_id}/positions"),
    ("POST", "/api/v1/reconciliations"),
}

MUTATION_ROUTES = {r.pattern for r in ROUTES if r.idempotency_required}


def test_route_table_is_frozen():
    assert {(r.method, r.pattern) for r in ROUTES} == EXPECTED_ROUTES


def test_mutation_routes_require_idempotency():
    assert {
        "/api/v1/jobs",
        "/api/v1/trade-plans/{plan_id}/approve",
        "/api/v1/orders",
        "/api/v1/orders/{order_id}/cancel",
        "/api/v1/reconciliations",
    } == MUTATION_ROUTES


# ---------------- 测试桩 ----------------


class FakeTaskQueue:
    """模拟 TaskQueue.submit：只做持久化落库，不真正执行脚本。"""

    def __init__(self, backend):
        self.backend = backend
        self.tasks: dict = {}

    def submit(self, task_id, on_output=None, on_complete=None,
               extra_args=None, idempotency_key=None):
        proceed, msg, job_id = self.backend.submit(
            task_id, list(extra_args or []), "scripts/noop.py", "compute",
            idempotency_key=idempotency_key,
        )
        if not proceed:
            return False, f"'{task_id}' {msg}", ""
        return True, f"已提交: {task_id}", job_id or task_id


class FakeTradingRepo:
    def __init__(self):
        self.plans = {
            "plan-1": {
                "plan_id": "plan-1",
                "status": "PENDING",
                "account_id": "main",
                "signal_date": "2026-08-29",
                "intended_trade_date": "2026-09-01",
                "orders": [{"symbol": "600000.SH"}, {"symbol": "000001.SZ"}],
            }
        }
        self.approved: list[str] = []

    def plan_detail(self, plan_id):
        plan = self.plans.get(plan_id)
        return dict(plan) if plan else None

    def approve_plan(self, plan_id):
        self.approved.append(plan_id)
        self.plans[plan_id]["status"] = "APPROVED"


class FakeOrderRepo:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions_from_fills(self, account_id):
        return dict(self._positions.get(account_id, {}))


ARTIFACT_RUNS = {
    "run-1": {
        "run_id": "run-1",
        "task": "factor_audit",
        "created_at": "2026-08-31T10:00:00",
        "status": "ok",
        "fingerprint": "fp-1",
        "params": {"horizon": 5},
        "metrics": {"ic": 0.05},
        "artifacts": [{"name": "summary.csv", "kind": "table", "rows": 10}],
    }
}


@pytest.fixture()
def control(tmp_path):
    from api.persistent_task_backend import PersistentTaskBackend
    from quart.infrastructure.db import Database
    from quart.infrastructure.job import JobRepository

    db = Database(tmp_path / "control.db")
    repo = JobRepository(db)
    service = ControlServiceV1(
        task_queue=FakeTaskQueue(PersistentTaskBackend(repo)),
        job_repo=repo,
        artifacts_getter=lambda run_id: ARTIFACT_RUNS.get(run_id),
        trading_repo=FakeTradingRepo(),
        order_repo=FakeOrderRepo({"paper-main": {"600000.SH": 700}}),
        freshness_probe=lambda: 0,
        snapshot_probe=lambda: "snap-1",
    )
    return ControlRouter(service), service


def post(router, path, body=None, key="idem-1"):
    headers = {"Idempotency-Key": key} if key else {}
    return router.dispatch("POST", path, body=body or {}, headers=headers)


# ---------------- 路由与错误语义 ----------------


def test_unknown_path_returns_404(control):
    router, _ = control
    resp = router.dispatch("GET", "/api/v1/nope")
    assert resp["status"] == 404
    assert resp["error"]["code"] == ApiErrorCode.NOT_FOUND


def test_wrong_method_returns_405(control):
    router, _ = control
    resp = router.dispatch("DELETE", "/api/v1/data/health")
    assert resp["status"] == 405
    assert resp["error"]["code"] == ApiErrorCode.METHOD_NOT_ALLOWED


def test_mutation_without_idempotency_key_is_400(control):
    router, _ = control
    resp = post(router, "/api/v1/jobs", {"job_type": "update_data"}, key=None)
    assert resp["status"] == 400
    assert resp["error"]["code"] == ApiErrorCode.VALIDATION_ERROR


def test_unhandled_exception_maps_to_500():
    class Broken:
        def data_health(self, params, body, idempotency_key):
            raise RuntimeError("boom")

    resp = ControlRouter(Broken()).dispatch("GET", "/api/v1/data/health")
    assert resp["status"] == 500
    assert resp["error"]["code"] == ApiErrorCode.INTERNAL
    assert set(resp["error"]) == {"code", "message", "details"}


# ---------------- jobs：提交与幂等重放 ----------------


def test_submit_job_and_replay_returns_same_job(control):
    router, service = control
    first = post(router, "/api/v1/jobs", {"job_type": "update_data", "args": []}, key="k-1")
    assert first["status"] == 201
    data = first["data"]
    assert data["job_type"] == "update_data"
    assert data["status"] == "QUEUED"
    assert data["idempotency_key"] == "k-1"

    second = post(router, "/api/v1/jobs", {"job_type": "update_data", "args": []}, key="k-1")
    assert second["status"] == 200
    assert second["data"]["job_id"] == data["job_id"]
    # 重放不产生重复任务
    assert service.job_repo.get_by_idempotency_key("k-1").job_id == data["job_id"]


def test_submit_job_validation(control):
    router, _ = control
    missing = post(router, "/api/v1/jobs", {}, key="k-2")
    assert missing["status"] == 400
    bad_args = post(router, "/api/v1/jobs", {"job_type": "update_data", "args": [1]}, key="k-3")
    assert bad_args["status"] == 400


def test_get_job_and_not_found(control):
    router, _ = control
    created = post(router, "/api/v1/jobs", {"job_type": "update_data"}, key="k-4")
    job_id = created["data"]["job_id"]

    got = router.dispatch("GET", f"/api/v1/jobs/{job_id}")
    assert got["status"] == 200
    assert got["data"]["job_id"] == job_id

    missing = router.dispatch("GET", "/api/v1/jobs/no-such-job")
    assert missing["status"] == 404


def test_job_events_for_persisted_only_job(control):
    router, _ = control
    created = post(router, "/api/v1/jobs", {"job_type": "update_data"}, key="k-5")
    job_id = created["data"]["job_id"]
    resp = router.dispatch("GET", f"/api/v1/jobs/{job_id}/events")
    assert resp["status"] == 200
    assert resp["data"]["lines"] == []
    assert resp["data"]["status"] == "QUEUED"


# ---------------- data health ----------------


@pytest.mark.parametrize(
    ("freshness", "ok"),
    [(None, False), (0, True), (2, True), (3, True), (6, False)],
)
def test_data_health_thresholds(freshness, ok):
    service = ControlServiceV1(
        freshness_probe=lambda: freshness, snapshot_probe=lambda: None
    )
    dto = service.data_health({}, {}, None)
    assert dto.ok is ok


def test_data_health_endpoint_envelope(control):
    router, _ = control
    resp = router.dispatch("GET", "/api/v1/data/health")
    assert resp["status"] == 200
    assert resp["data"]["ok"] is True
    assert resp["data"]["snapshot_id"] == "snap-1"


# ---------------- artifacts / trade plans ----------------


def test_get_artifact(control):
    router, _ = control
    resp = router.dispatch("GET", "/api/v1/artifacts/run-1")
    assert resp["status"] == 200
    assert resp["data"]["files"] == ["summary.csv"]
    assert resp["data"]["fingerprint"] == "fp-1"
    assert resp["data"]["metadata"]["status"] == "ok"

    missing = router.dispatch("GET", "/api/v1/artifacts/run-missing")
    assert missing["status"] == 404


def test_approve_trade_plan(control):
    router, service = control
    resp = post(router, "/api/v1/trade-plans/plan-1/approve", {}, key="k-6")
    assert resp["status"] == 200
    assert resp["data"]["status"] == "APPROVED"
    assert resp["data"]["order_count"] == 2
    assert service.trading_repo.approved == ["plan-1"]

    missing = post(router, "/api/v1/trade-plans/plan-x/approve", {}, key="k-7")
    assert missing["status"] == 404


# ---------------- 持仓（OMS-001 接线）与剩余占位 ----------------


def test_positions_from_oms_fills(control):
    router, _ = control
    resp = router.dispatch("GET", "/api/v1/accounts/paper-main/positions")
    assert resp["status"] == 200
    assert resp["data"]["account_id"] == "paper-main"
    assert resp["data"]["positions"] == {"600000.SH": 700}
    assert resp["data"]["derived_from"] == "oms_fills"


def test_broker_placeholders_return_503(control):
    router, _ = control
    assert post(router, "/api/v1/orders", {"symbol": "600000.SH"})["status"] == 503
    assert post(router, "/api/v1/orders/ord-1/cancel", {})["status"] == 503
    assert post(router, "/api/v1/reconciliations", {})["status"] == 503


# ---------------- DTO 字段合同 ----------------

EXPECTED_DTO_FIELDS = {
    "ErrorDTO": {"code", "message", "details"},
    "HealthDTO": {"ok", "freshness_days", "message", "snapshot_id", "rule_book_version"},
    "JobDTO": {
        "job_id", "job_type", "status", "idempotency_key", "attempts",
        "created_at", "started_at", "finished_at", "error",
    },
    "JobEventsDTO": {"job_id", "status", "line_count", "lines"},
    "ArtifactRunDTO": {"run_id", "task", "created_at", "fingerprint", "files", "metadata"},
    "TradePlanDTO": {
        "plan_id", "status", "account_id", "signal_date",
        "intended_trade_date", "order_count",
    },
    "PositionsDTO": {"account_id", "positions", "derived_from"},
}


def test_dto_fields_are_frozen():
    actual = {dto.__name__: {f.name for f in dataclasses.fields(dto)} for dto in CONTRACT_DTOS}
    assert actual == EXPECTED_DTO_FIELDS


def test_error_codes_map_to_status():
    assert ApiError(ApiErrorCode.VALIDATION_ERROR, "x").status == 400
    assert ApiError(ApiErrorCode.NOT_FOUND, "x").status == 404
    assert ApiError(ApiErrorCode.CONFLICT, "x").status == 409
    assert ApiError(ApiErrorCode.METHOD_NOT_ALLOWED, "x").status == 405
    assert ApiError(ApiErrorCode.SERVICE_UNAVAILABLE, "x").status == 503
    assert ApiError(ApiErrorCode.INTERNAL, "x").status == 500


# ---------------- OpenAPI 冻结规范 ----------------


def test_openapi_frozen_spec_matches_generated():
    frozen = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert frozen == generate_openapi(), (
        "OpenAPI 合同与代码不一致：请显式运行 "
        "`uv run python -c 'from api.control.openapi import write_openapi; write_openapi()'` "
        "更新冻结文件，并确认这是有意的合同变更"
    )
