"""BROKER-001 验收测试：PaperBroker 持久化、重启恢复与故障注入。

验收标准（协调文档 §12）：
- 重启恢复：状态全部来自 OMS，新进程实例直接读库继续；
- 重复回报不重复入账（同 ``broker_fill_id`` 幂等）；
- 故障注入：报单拒绝（reject）与确认丢失（drop_ack）都有确定性落库结论，
  超时恢复必须先按 ``client_order_id`` 查询，再补发回报。
"""
from __future__ import annotations

import pytest

from quart.broker.models import BrokerOrderRequest
from quart.broker.persistent import PaperFaultConfig, PersistentPaperBroker
from quart.domain import OrderStatus
from quart.infrastructure.db import Database
from quart.oms import OrderRepository


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "broker.db"


def make_broker(db_path, fault: PaperFaultConfig | None = None) -> PersistentPaperBroker:
    return PersistentPaperBroker(OrderRepository(Database(db_path)), fault=fault)


def make_request(client_order_id: str = "client-1", quantity: int = 1000) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        symbol="600000.SH",
        side="BUY",
        quantity=quantity,
        client_order_id=client_order_id,
    )


# ---------------- 正常报单路径 ----------------


def test_submit_order_reaches_submitted(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request())
    assert order.status is OrderStatus.SUBMITTED
    assert order.broker_order_id
    statuses = [r["status"] for r in broker.oms.list_reports(order.client_order_id)]
    assert statuses == ["RISK_APPROVED", "SUBMITTING", "SUBMITTED"]


def test_submit_retry_is_idempotent(db_path):
    broker = make_broker(db_path)
    request = make_request()
    first = broker.submit_order(request)
    second = broker.submit_order(request)
    assert second.client_order_id == first.client_order_id
    assert second.status is OrderStatus.SUBMITTED
    assert len(broker.oms.list_orders()) == 1
    assert len(broker.oms.list_reports(first.client_order_id)) == 3


# ---------------- 故障注入 ----------------


def test_fault_reject_lands_terminal_state(db_path):
    broker = make_broker(db_path, PaperFaultConfig(submit_outcome="reject"))
    order = broker.submit_order(make_request())
    assert order.status is OrderStatus.REJECTED
    assert order.is_terminal
    assert order.status_reason and "拒绝" in order.status_reason
    # 重试不应产生新订单或新回报
    again = broker.submit_order(make_request())
    assert again.status is OrderStatus.REJECTED
    assert len(broker.oms.list_orders()) == 1


def test_fault_drop_ack_stops_at_submitting_then_recovers(db_path):
    broker = make_broker(db_path, PaperFaultConfig(submit_outcome="drop_ack"))
    order = broker.submit_order(make_request())
    assert order.status is OrderStatus.SUBMITTING
    assert order.broker_order_id is None

    # 模拟重启：新实例读库，先按 client_order_id 查询
    recovered = make_broker(db_path)
    active = recovered.active_orders()
    assert [o.client_order_id for o in active] == [order.client_order_id]
    queried = recovered.get_order(order.client_order_id)
    assert queried is not None and queried.status is OrderStatus.SUBMITTING

    # 查询确认报单已送达后补发 SUBMITTED
    confirmed = recovered.confirm_submitted(order.client_order_id)
    assert confirmed.status is OrderStatus.SUBMITTED
    assert confirmed.broker_order_id
    # 补发幂等
    again = recovered.confirm_submitted(order.client_order_id)
    assert again.status is OrderStatus.SUBMITTED
    assert len(recovered.oms.list_reports(order.client_order_id)) == 3


def test_unknown_fault_mode_rejected():
    with pytest.raises(ValueError):
        PaperFaultConfig(submit_outcome="explode")


# ---------------- 成交与重复回报 ----------------


def test_fill_lifecycle_and_positions(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request())
    order = broker.apply_fill(
        order.client_order_id, 400, 10.0,
        trade_date="2026-08-31", trade_time="10:00:00", broker_fill_id="F1",
    )
    assert order.status is OrderStatus.PARTIALLY_FILLED
    order = broker.apply_fill(
        order.client_order_id, 600, 11.0,
        trade_date="2026-08-31", trade_time="10:05:00", broker_fill_id="F2",
    )
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1000
    assert float(order.average_fill_price) == pytest.approx(10.6)
    assert broker.positions() == {"600000.SH": 1000}
    assert len(broker.oms.list_fills(account_id="paper")) == 2


def test_duplicate_fill_replay_does_not_double_book(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request())
    broker.apply_fill(
        order.client_order_id, 1000, 10.0,
        trade_date="2026-08-31", trade_time="10:00:00", broker_fill_id="F1",
    )
    # 重复回报（同 broker_fill_id）：幂等，不重复入账
    replayed = broker.apply_fill(
        order.client_order_id, 1000, 10.0,
        trade_date="2026-08-31", trade_time="10:00:00", broker_fill_id="F1",
    )
    assert replayed.status is OrderStatus.FILLED
    assert replayed.filled_quantity == 1000
    assert len(broker.oms.list_fills(account_id="paper")) == 1
    assert broker.positions() == {"600000.SH": 1000}


def test_restart_recovery_replays_fill_without_double_booking(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request())
    broker.apply_fill(
        order.client_order_id, 400, 10.0,
        trade_date="2026-08-31", trade_time="10:00:00", broker_fill_id="F1",
    )

    # 进程重启：新实例收到同一条成交回报重放
    recovered = make_broker(db_path)
    replayed = recovered.apply_fill(
        order.client_order_id, 400, 10.0,
        trade_date="2026-08-31", trade_time="10:00:00", broker_fill_id="F1",
    )
    assert replayed.status is OrderStatus.PARTIALLY_FILLED
    assert replayed.filled_quantity == 400
    assert len(recovered.oms.list_fills(account_id="paper")) == 1
    assert recovered.positions() == {"600000.SH": 400}


def test_fill_validation(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request())
    with pytest.raises(ValueError):
        broker.apply_fill(order.client_order_id, 1001, 10.0)
    with pytest.raises(ValueError):
        broker.apply_fill(order.client_order_id, 0, 10.0)
    with pytest.raises(KeyError):
        broker.apply_fill("ghost-order", 100, 10.0)


# ---------------- 撤单 ----------------


def test_cancel_and_cancel_idempotency(db_path):
    broker = make_broker(db_path)
    order = broker.submit_order(make_request("client-cancel"))
    canceled = broker.cancel_order(order.client_order_id)
    assert canceled.status is OrderStatus.CANCELED
    # 重复撤单幂等返回
    again = broker.cancel_order(order.client_order_id)
    assert again.status is OrderStatus.CANCELED

    filled = broker.submit_order(make_request("client-filled"))
    broker.apply_fill(filled.client_order_id, 1000, 10.0, broker_fill_id="F-x")
    with pytest.raises(ValueError):
        broker.cancel_order(filled.client_order_id)
