import pytest

from quart.broker import BrokerAdapter, BrokerOrderRequest, OrderStatus, PaperBrokerAdapter


def test_paper_broker_partial_fill_state_machine():
    broker = PaperBrokerAdapter()
    assert isinstance(broker, BrokerAdapter)
    order = broker.submit_order(BrokerOrderRequest("600000", "BUY", 1_000, 10.0, planned_order_id=7))
    assert order.status == OrderStatus.SUBMITTED

    first = broker.apply_fill(order.broker_order_id, 400, 10.0, trade_date="2026-08-31")
    assert first.planned_order_id == 7
    partial = broker.get_order(order.broker_order_id)
    assert partial.status == OrderStatus.PARTIALLY_FILLED
    assert partial.remaining_quantity == 600

    broker.apply_fill(order.broker_order_id, 600, 10.2, trade_date="2026-08-31")
    completed = broker.get_order(order.broker_order_id)
    assert completed.status == OrderStatus.FILLED
    assert completed.average_fill_price == pytest.approx(10.12)


def test_paper_broker_rejects_overfill_and_cancel_after_fill():
    broker = PaperBrokerAdapter()
    order = broker.submit_order(BrokerOrderRequest("600000", "SELL", 100))
    with pytest.raises(ValueError, match="超过"):
        broker.apply_fill(order.broker_order_id, 200, 10.0)
    broker.apply_fill(order.broker_order_id, 100, 10.0)
    with pytest.raises(ValueError, match="不可撤销"):
        broker.cancel_order(order.broker_order_id)
