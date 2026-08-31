"""OMS-001 验收测试：持久化订单状态机 + 成交入账。

验收标准（协调文档 §12）：重复回报/重启不重复入账。
"""
from __future__ import annotations

import pytest

from quart.domain import (
    OrderIntent,
    OrderStatus,
    OrderTransitionError,
    RiskDecision,
    RiskRuleResult,
    create_order_from_risk_decision,
    make_execution_report,
)
from quart.infrastructure.db import Database
from quart.oms import OrderRepository


def make_approved_order(
    symbol: str = "600000.SH",
    side: str = "BUY",
    quantity: int = 1000,
    account: str = "paper-main",
    intent_id: str | None = None,
):
    """构造一笔通过风控（RISK_APPROVED）的订单。"""
    intent = OrderIntent.create(
        account_id=account,
        environment="paper",
        symbol=symbol,
        side=side,
        quantity=quantity,
        source="TEST",
        intent_id=intent_id,
    )
    decision = RiskDecision.allow(intent, limit_version="test-v1")
    return create_order_from_risk_decision(intent, decision)


def make_denied_order(account: str = "paper-main"):
    intent = OrderIntent.create(
        account_id=account,
        environment="paper",
        symbol="600000.SH",
        side="BUY",
        quantity=1000,
        source="TEST",
    )
    decision = RiskDecision.deny(
        intent,
        rules=(RiskRuleResult(rule_id="STATE_GATE", outcome="DENY", message="HALTED"),),
        limit_version="test-v1",
        reason="风险状态闸门拦截",
    )
    return create_order_from_risk_decision(intent, decision)


@pytest.fixture()
def repo(tmp_path):
    return OrderRepository(Database(tmp_path / "oms.db"))


def run_to_submitted(repo, order, broker_order_id="brk-1"):
    repo.create_order(order)
    order = repo.apply_report(
        make_execution_report(
            order,
            status="SUBMITTING",
            source="TEST",
            idempotency_key=f"{order.client_order_id}:submitting",
        )
    )
    return repo.apply_report(
        make_execution_report(
            order,
            status="SUBMITTED",
            source="TEST",
            idempotency_key=f"{order.client_order_id}:submitted",
            broker_order_id=broker_order_id,
        )
    )


# ---------------- 订单创建幂等 ----------------


def test_create_order_is_idempotent(repo):
    order = make_approved_order(intent_id="intent-dup-1")
    first = repo.create_order(order)
    second = repo.create_order(order)
    assert first.client_order_id == second.client_order_id
    assert len(repo.list_orders()) == 1


def test_resubmit_same_intent_does_not_duplicate(repo):
    """同一 intent（同幂等键）的重复提交返回既有订单。"""
    first = repo.create_order(make_approved_order(intent_id="intent-resub"))
    rebuilt = make_approved_order(intent_id="intent-resub")
    second = repo.create_order(rebuilt)
    assert second.client_order_id == first.client_order_id
    assert len(repo.list_orders()) == 1


def test_conflicting_identity_is_rejected(repo):
    original = make_approved_order(intent_id="intent-conflict", quantity=1000)
    repo.create_order(original)
    intent = OrderIntent.create(
        account_id="paper-main",
        environment="paper",
        symbol="600000.SH",
        side="BUY",
        quantity=2000,
        source="TEST",
        intent_id="intent-conflict",
    )
    decision = RiskDecision.allow(intent, limit_version="test-v1")
    conflicting = create_order_from_risk_decision(intent, decision)
    with pytest.raises(ValueError, match="不一致"):
        repo.create_order(conflicting)


def test_denied_order_is_terminal(repo):
    order = make_denied_order()
    repo.create_order(order)
    stored = repo.get_order(order.client_order_id)
    assert stored is not None
    assert stored.status is OrderStatus.DENIED
    assert repo.list_active_orders() == []


# ---------------- 状态机推进与成交入账 ----------------


def test_full_lifecycle_with_fills(repo):
    order = make_approved_order()
    order = run_to_submitted(repo, order)
    fill1 = make_execution_report(
        order,
        status="PARTIALLY_FILLED",
        source="TEST",
        idempotency_key="fill:1",
        cumulative_filled_quantity=400,
        last_filled_quantity=400,
        last_fill_price="10.5",
        broker_order_id="brk-1",
    )
    order = repo.apply_report(fill1)
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 400

    fill2 = make_execution_report(
        order,
        status="FILLED",
        source="TEST",
        idempotency_key="fill:2",
        cumulative_filled_quantity=1000,
        last_filled_quantity=600,
        last_fill_price="11.0",
        broker_order_id="brk-1",
    )
    order = repo.apply_report(fill2)
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1000
    # 加权均价 = (10.5*400 + 11.0*600) / 1000 = 10.8
    assert float(order.average_fill_price) == pytest.approx(10.8)

    fills = repo.list_fills(account_id="paper-main")
    assert [f.quantity for f in fills] == [400, 600]
    assert len(repo.list_reports(order.client_order_id)) == 4


def test_illegal_transition_is_rejected_and_persists_nothing(repo):
    order = make_approved_order()
    repo.create_order(order)
    bad_report = make_execution_report(
        order,
        status="PARTIALLY_FILLED",
        source="TEST",
        idempotency_key="fill:bad",
        cumulative_filled_quantity=400,
        last_filled_quantity=400,
        last_fill_price="10.5",
    )
    with pytest.raises(OrderTransitionError):
        repo.apply_report(bad_report)
    stored = repo.get_order(order.client_order_id)
    assert stored is not None
    assert stored.status is OrderStatus.RISK_APPROVED
    assert repo.list_reports(order.client_order_id) == []
    assert repo.list_fills() == []


def test_report_for_unknown_order_raises(repo):
    persisted = make_approved_order(intent_id="intent-known")
    repo.create_order(persisted)
    ghost = make_approved_order(intent_id="intent-ghost")
    report = make_execution_report(
        ghost, status="SUBMITTING", source="TEST", idempotency_key="k:ghost"
    )
    with pytest.raises(LookupError):
        repo.apply_report(report)


# ---------------- 验收：重复回报不重复入账 ----------------


def test_duplicate_report_does_not_double_book(repo):
    order = make_approved_order()
    order = run_to_submitted(repo, order)
    fill = make_execution_report(
        order,
        status="PARTIALLY_FILLED",
        source="TEST",
        idempotency_key="fill:dup",
        cumulative_filled_quantity=500,
        last_filled_quantity=500,
        last_fill_price="10.0",
        broker_order_id="brk-1",
    )
    first = repo.apply_report(fill)
    replayed = repo.apply_report(fill)
    third = repo.apply_report(fill)
    assert first.filled_quantity == replayed.filled_quantity == third.filled_quantity == 500
    assert len(repo.list_fills(account_id="paper-main")) == 1
    # 重复回报之后仍可继续成交
    rest = make_execution_report(
        replayed,
        status="FILLED",
        source="TEST",
        idempotency_key="fill:rest",
        cumulative_filled_quantity=1000,
        last_filled_quantity=500,
        last_fill_price="10.2",
        broker_order_id="brk-1",
    )
    final = repo.apply_report(rest)
    assert final.status is OrderStatus.FILLED
    assert len(repo.list_fills(account_id="paper-main")) == 2


# ---------------- 验收：重启恢复不重复入账 ----------------


def test_restart_recovery_does_not_double_book(tmp_path):
    path = tmp_path / "oms.db"
    repo = OrderRepository(Database(path))
    order = make_approved_order(intent_id="intent-restart")
    order = run_to_submitted(repo, order)
    fill1 = make_execution_report(
        order,
        status="PARTIALLY_FILLED",
        source="TEST",
        idempotency_key="fill:r1",
        cumulative_filled_quantity=400,
        last_filled_quantity=400,
        last_fill_price="10.5",
        broker_order_id="brk-1",
    )
    order = repo.apply_report(fill1)

    # 模拟进程崩溃：丢弃引用，用同一数据库文件重启
    restarted = OrderRepository(Database(path))
    active = restarted.list_active_orders()
    assert [o.client_order_id for o in active] == [order.client_order_id]
    recovered = active[0]
    assert recovered.status is OrderStatus.PARTIALLY_FILLED
    assert recovered.filled_quantity == 400

    # 重放崩溃前的回报（幂等）+ 新成交
    assert restarted.apply_report(fill1).filled_quantity == 400
    fill2 = make_execution_report(
        recovered,
        status="FILLED",
        source="TEST",
        idempotency_key="fill:r2",
        cumulative_filled_quantity=1000,
        last_filled_quantity=600,
        last_fill_price="11.0",
        broker_order_id="brk-1",
    )
    final = restarted.apply_report(fill2)
    assert final.status is OrderStatus.FILLED
    fills = restarted.list_fills(account_id="paper-main")
    assert [f.quantity for f in fills] == [400, 600]
    assert restarted.list_active_orders() == []


def test_crash_at_submitting_recovers(tmp_path):
    """崩溃在 SUBMITTING：重启后可查询并按券商回报继续推进。"""
    path = tmp_path / "oms.db"
    repo = OrderRepository(Database(path))
    order = make_approved_order(intent_id="intent-submit-crash")
    repo.create_order(order)
    order = repo.apply_report(
        make_execution_report(
            order, status="SUBMITTING", source="TEST", idempotency_key="k:submitting"
        )
    )

    restarted = OrderRepository(Database(path))
    (active,) = restarted.list_active_orders()
    assert active.status is OrderStatus.SUBMITTING
    # 查询券商后确认已报出：继续推进
    order = restarted.apply_report(
        make_execution_report(
            active,
            status="SUBMITTED",
            source="TEST",
            idempotency_key="k:submitted",
            broker_order_id="brk-9",
        )
    )
    assert order.status is OrderStatus.SUBMITTED
    assert order.broker_order_id == "brk-9"


# ---------------- 派生持仓查询模型 ----------------


def test_positions_from_fills(repo):
    buy = make_approved_order(symbol="600000.SH", quantity=1000, intent_id="intent-pos-1")
    buy = run_to_submitted(repo, buy, broker_order_id="brk-p1")
    buy = repo.apply_report(
        make_execution_report(
            buy,
            status="FILLED",
            source="TEST",
            idempotency_key="fill:p1",
            cumulative_filled_quantity=1000,
            last_filled_quantity=1000,
            last_fill_price="10.0",
            broker_order_id="brk-p1",
        )
    )
    sell = make_approved_order(
        symbol="600000.SH", side="SELL", quantity=300, intent_id="intent-pos-2"
    )
    sell = run_to_submitted(repo, sell, broker_order_id="brk-p2")
    repo.apply_report(
        make_execution_report(
            sell,
            status="FILLED",
            source="TEST",
            idempotency_key="fill:p2",
            cumulative_filled_quantity=300,
            last_filled_quantity=300,
            last_fill_price="10.5",
            broker_order_id="brk-p2",
        )
    )
    assert repo.positions_from_fills("paper-main") == {"600000.SH": 700}


# ---------------- 迁移顺序无关 ----------------


def _tables(db: Database) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {r["name"] for r in rows}


def test_migration_order_independence(tmp_path):
    from quart.infrastructure.job import JobRepository
    from quart.risk import RiskRepository

    first_risk = Database(tmp_path / "a.db")
    RiskRepository(first_risk).migrate()
    OrderRepository(first_risk).migrate()

    first_oms = Database(tmp_path / "b.db")
    OrderRepository(first_oms).migrate()
    JobRepository(first_oms).migrate()

    for db in (first_risk, first_oms):
        tables = _tables(db)
        assert {"oms_orders", "oms_execution_reports", "oms_fills"} <= tables
        assert "jobs" in tables
        assert {"risk_states", "risk_decisions"} <= tables
