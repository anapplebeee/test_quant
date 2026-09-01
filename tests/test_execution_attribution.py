from __future__ import annotations

from datetime import datetime

import pytest

from quart.broker.models import BrokerOrderRequest
from quart.broker.persistent import PersistentPaperBroker
from quart.execution.attribution import attribute_execution, attribute_paper_account
from quart.infrastructure.db import Database
from quart.oms import OrderRepository


def _submit_and_fill(
    broker: PersistentPaperBroker,
    *,
    client_order_id: str,
    side: str,
    quantity: int,
    filled: int,
    reference: float,
    price: float,
) -> None:
    order_time = datetime.fromisoformat("2026-09-01T01:30:00+00:00")
    broker.submit_order(BrokerOrderRequest(
        symbol="600000.SH",
        side=side,
        quantity=quantity,
        limit_price=reference,
        client_order_id=client_order_id,
        account_id=broker.account_id,
        business_time=order_time,
    ))
    broker.apply_fill(
        client_order_id,
        filled,
        price,
        trade_date="2026-09-01",
        trade_time="10:00:00",
        broker_fill_id=f"fill:{client_order_id}",
    )


def test_paper_attribution_links_order_price_quantity_and_latency(tmp_path):
    repository = OrderRepository(Database(tmp_path / "attribution.db"))
    broker = PersistentPaperBroker(repository, account_id="paper-a")
    _submit_and_fill(
        broker, client_order_id="buy", side="BUY", quantity=100, filled=100,
        reference=10.0, price=10.2,
    )
    _submit_and_fill(
        broker, client_order_id="sell", side="SELL", quantity=100, filled=50,
        reference=10.0, price=9.8,
    )

    rows, summary = attribute_paper_account(repository, "paper-a")

    assert list(rows["client_order_id"]) == ["buy", "sell"]
    buy, sell = rows.iloc[0], rows.iloc[1]
    assert buy["adverse_slippage_bps"] == pytest.approx(200.0)
    assert sell["adverse_slippage_bps"] == pytest.approx((10 / 9.8 - 1) * 10_000)
    assert sell["remaining_quantity"] == 50
    assert sell["unfilled_reference_notional"] == pytest.approx(500.0)
    assert buy["first_fill_latency_seconds"] == pytest.approx(1800.0)
    assert summary.quantity_fill_rate == pytest.approx(0.75)
    assert summary.mean_adverse_slippage_bps == pytest.approx(
        (200 + (10 / 9.8 - 1) * 10_000) / 2
    )
    assert summary.total_unfilled_reference_notional == pytest.approx(500.0)
    assert summary.median_first_fill_latency_seconds == pytest.approx(1800.0)


def test_attribution_rejects_a_fill_time_before_its_order(tmp_path):
    repository = OrderRepository(Database(tmp_path / "attribution.db"))
    broker = PersistentPaperBroker(repository, account_id="paper-a")
    _submit_and_fill(
        broker, client_order_id="buy", side="BUY", quantity=100, filled=100,
        reference=10.0, price=10.1,
    )
    order = repository.get_order("buy")
    assert order is not None

    with pytest.raises(ValueError, match="成交时间早于委托时间"):
        attribute_execution(
            [order],
            first_fill_times={"buy": datetime.fromisoformat("2026-09-01T01:29:59+00:00")},
        )
