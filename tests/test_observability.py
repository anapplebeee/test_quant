"""OBS-001 验收测试：结构化日志 + 核心指标。

验收标准（协调文档 §12）：可定位 job/order/reconcile 全链路——
每条日志携带 §13.2 关联字段，可按 job_id / order_id 检索；核心指标
（Job 排队/运行时长、失败率、委托拒绝率、成交率、风控拒绝等）可从
平台表派生。
"""
from __future__ import annotations

import json

import pytest
from loguru import logger

from quart.domain import (
    OrderIntent,
    RiskDecision,
    create_order_from_risk_decision,
    make_execution_report,
)
from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository
from quart.observability import (
    TRACE_FIELDS,
    MetricsRepository,
    collect_core_metrics,
    configure_structured_logging,
    log_event,
    trace_context,
)
from quart.oms import OrderRepository


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "obs.db")


def capture_events():
    """挂一个内存捕获，返回 (记录列表, 清理函数)。"""
    records: list[dict] = []
    sink_id = logger.add(lambda msg: records.append(msg.record), level="DEBUG")
    return records, lambda: logger.remove(sink_id)


# ---------------- 结构化日志 ----------------


def test_trace_fields_cover_architecture_requirements():
    required = {
        "trace_id", "job_id", "run_id", "account_id",
        "plan_id", "order_id", "broker_order_id", "strategy", "environment",
    }
    assert required <= set(TRACE_FIELDS)


def test_log_event_writes_json_line(tmp_path):
    log_file = tmp_path / "platform.jsonl"
    sink_ids = configure_structured_logging(log_file)
    try:
        log_event(
            "order.transition",
            order_id="client-1",
            account_id="paper",
            environment="paper",
            status="SUBMITTED",
        )
    finally:
        for sid in sink_ids:
            logger.remove(sid)
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])["record"]
    extra = record["extra"]
    assert extra["event"] == "order.transition"
    assert extra["order_id"] == "client-1"
    assert extra["environment"] == "paper"


def test_trace_context_merges_into_events(tmp_path):
    log_file = tmp_path / "trace.jsonl"
    sink_ids = configure_structured_logging(log_file)
    try:
        with trace_context(trace_id="trace-xyz", job_id="job-1", strategy="lowvol_indz"):
            log_event("job.step", step="fetch")
    finally:
        for sid in sink_ids:
            logger.remove(sid)
    extra = json.loads(log_file.read_text(encoding="utf-8"))["record"]["extra"]
    assert extra["trace_id"] == "trace-xyz"
    assert extra["job_id"] == "job-1"
    assert extra["strategy"] == "lowvol_indz"
    assert extra["step"] == "fetch"


def test_oms_transition_emits_structured_event(db):
    """订单状态推进落库时同时发出结构化事件（order 全链路可定位）。"""
    records, cleanup = capture_events()
    try:
        repo = OrderRepository(db)
        intent = OrderIntent.create(
            account_id="paper", environment="paper", symbol="600000.SH",
            side="BUY", quantity=100, source="TEST",
        )
        decision = RiskDecision.allow(intent, limit_version="test-v1")
        order = create_order_from_risk_decision(intent, decision)
        repo.create_order(order)
        repo.apply_report(
            make_execution_report(
                order, status="SUBMITTING", source="TEST",
                idempotency_key=f"{order.client_order_id}:submitting",
            )
        )
    finally:
        cleanup()
    events = [r["extra"].get("event") for r in records if r["extra"].get("event")]
    assert "order.transition" in events
    transition = next(r for r in records if r["extra"].get("event") == "order.transition")
    assert transition["extra"]["order_id"] == order.client_order_id
    assert transition["extra"]["status"] == "SUBMITTING"
    assert transition["extra"]["environment"] == "paper"


# ---------------- 自定义指标仓库 ----------------


def test_metrics_repository_record_latest_history(db):
    metrics = MetricsRepository(db)
    metrics.record("data.freshness_days", 1.0, labels={"dataset": "daily"})
    metrics.record("data.freshness_days", 2.0, labels={"dataset": "daily"})
    latest = metrics.latest("data.freshness_days")
    assert latest is not None
    value, labels = latest
    assert value == 2.0
    assert labels == {"dataset": "daily"}
    history = metrics.history("data.freshness_days")
    assert [h["value"] for h in history] == [2.0, 1.0]
    assert metrics.latest("missing.metric") is None


def test_obs_table_created_regardless_of_initializer(db):
    """任意仓储先初始化都能建出 obs_metrics（统一 migration 列表）。"""
    OrderRepository(db).migrate()
    MetricsRepository(db).record("probe", 1.0)
    assert MetricsRepository(db).latest("probe") == (1.0, {})


# ---------------- 派生核心指标 ----------------


def _seed_jobs(db):
    repo = JobRepository(db)
    ok = repo.create("update_data", payload={"script": "x", "args": [], "resource": "cpu"})
    bad = repo.create("factor_audit", payload={"script": "y", "args": [], "resource": "cpu"})
    assert repo.claim_job(ok.job_id, "w1")
    repo.succeed(ok.job_id, "w1", result={})
    assert repo.claim_job(bad.job_id, "w1")
    repo.fail(bad.job_id, "w1", "boom")
    return repo


def _seed_orders(db):
    repo = OrderRepository(db)
    # 一笔完整成交
    intent = OrderIntent.create(
        account_id="paper", environment="paper", symbol="600000.SH",
        side="BUY", quantity=100, source="TEST",
    )
    decision = RiskDecision.allow(intent, limit_version="test-v1")
    order = create_order_from_risk_decision(intent, decision)
    repo.create_order(order)
    order = repo.apply_report(
        make_execution_report(
            order, status="SUBMITTING", source="TEST",
            idempotency_key=f"{order.client_order_id}:submitting",
        )
    )
    order = repo.apply_report(
        make_execution_report(
            order, status="SUBMITTED", source="TEST",
            idempotency_key=f"{order.client_order_id}:submitted",
            broker_order_id="brk-1",
        )
    )
    repo.apply_report(
        make_execution_report(
            order, status="FILLED", source="TEST",
            idempotency_key=f"{order.client_order_id}:filled",
            cumulative_filled_quantity=100, last_filled_quantity=100,
            last_fill_price=10.0, broker_order_id="brk-1",
        )
    )
    # 一笔被券商拒绝
    intent2 = OrderIntent.create(
        account_id="paper", environment="paper", symbol="000001.SZ",
        side="BUY", quantity=200, source="TEST",
    )
    decision2 = RiskDecision.allow(intent2, limit_version="test-v1")
    order2 = create_order_from_risk_decision(intent2, decision2)
    repo.create_order(order2)
    order2 = repo.apply_report(
        make_execution_report(
            order2, status="SUBMITTING", source="TEST",
            idempotency_key=f"{order2.client_order_id}:submitting",
        )
    )
    repo.apply_report(
        make_execution_report(
            order2, status="REJECTED", source="TEST",
            idempotency_key=f"{order2.client_order_id}:rejected",
            reason="券商拒绝",
        )
    )
    return repo


def _seed_risk(db):
    now = "2026-08-31T00:00:00+00:00"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO risk_decisions (decision_id, idempotency_key, intent_id, "
            "account_id, environment, status, requested_quantity, approved_quantity, "
            "limit_version, reason, business_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("d1", "k1", "i1", "paper", "paper", "ALLOW", 100, 100, "v1", "", now, now),
        )
        conn.execute(
            "INSERT INTO risk_decisions (decision_id, idempotency_key, intent_id, "
            "account_id, environment, status, requested_quantity, approved_quantity, "
            "limit_version, reason, business_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("d2", "k2", "i2", "paper", "paper", "DENY", 100, 0, "v1", "HALTED", now, now),
        )
        conn.execute(
            "INSERT INTO risk_state_history (account_id, state, reason, operator, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("paper", "HALTED", "熔断", "system", now),
        )
        conn.commit()


def test_collect_core_metrics(db):
    _seed_jobs(db)
    _seed_orders(db)
    _seed_risk(db)

    snapshot = collect_core_metrics(db)
    job, order, risk = snapshot["job"], snapshot["order"], snapshot["risk"]

    assert job["jobs_total"] == 2
    assert job["jobs_succeeded"] == 1
    assert job["jobs_failed"] == 1
    assert job["job_failure_rate"] == 0.5
    assert job["jobs_active"] == 0
    assert job["job_avg_queue_seconds"] >= 0
    assert job["job_avg_run_seconds"] >= 0

    assert order["orders_total"] == 2
    assert order["orders_filled"] == 1
    assert order["order_fill_rate"] == 0.5
    assert order["orders_rejected"] == 1
    assert order["order_reject_rate"] == 0.5
    assert order["orders_active"] == 0

    assert risk["risk_decisions_total"] == 2
    assert risk["risk_decisions_denied"] == 1
    assert risk["risk_state_transitions"] == 1


def test_persistent_backend_emits_job_chain_events(db):
    from api.persistent_task_backend import PersistentTaskBackend

    records, cleanup = capture_events()
    try:
        backend = PersistentTaskBackend(JobRepository(db))
        ok, _, job_id = backend.submit("update_data", ["--full"], "scripts/update_data.py", "cpu")
        assert ok and job_id
        assert backend.claim_and_run(job_id)
        backend.finish(job_id, 0)

        ok2, _, job_id2 = backend.submit("factor_audit", ["--x"], "scripts/factor_audit.py", "cpu")
        assert ok2 and job_id2
        assert backend.claim_and_run(job_id2)
        backend.finish(job_id2, 1, error="boom")

        ok3, _, job_id3 = backend.submit("train_ml", [], "scripts/train_ml.py", "gpu")
        assert ok3 and job_id3
        backend.cancel(job_id3, "用户取消")
    finally:
        cleanup()
    events = [r["extra"].get("event") for r in records if r["extra"].get("event")]
    assert events.count("job.created") == 3
    assert events.count("job.started") == 2
    assert "job.succeeded" in events
    assert "job.failed" in events
    assert "job.cancelled" in events
    job_ids = {r["extra"].get("job_id") for r in records}
    assert {job_id, job_id2, job_id3} <= job_ids


def test_collect_core_metrics_on_empty_db(db):
    snapshot = collect_core_metrics(db)
    assert snapshot["job"]["jobs_total"] == 0
    assert snapshot["order"]["orders_total"] == 0
    assert snapshot["risk"]["risk_decisions_total"] == 0
