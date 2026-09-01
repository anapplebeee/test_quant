from __future__ import annotations

import pytest

from quart.broker.models import BrokerOrderRequest
from quart.broker.persistent import PersistentPaperBroker
from quart.execution.paper_calibration import (
    calibrate_paper_account,
    calibrate_paper_execution,
)
from quart.infrastructure.db import Database
from quart.oms import OrderRepository


def _fill(
    broker: PersistentPaperBroker,
    *,
    client_order_id: str,
    side: str,
    quantity: int = 100,
    filled: int | None = None,
    reference: float = 10.0,
    price: float = 10.0,
) -> None:
    broker.submit_order(BrokerOrderRequest(
        symbol="600000.SH",
        side=side,
        quantity=quantity,
        limit_price=reference,
        client_order_id=client_order_id,
        account_id=broker.account_id,
    ))
    broker.apply_fill(
        client_order_id,
        filled or quantity,
        price,
        trade_date="2026-09-01",
        broker_fill_id=f"fill:{client_order_id}",
    )


def test_paper_calibration_unifies_buy_sell_adverse_slippage_and_fill_rate(tmp_path):
    repository = OrderRepository(Database(tmp_path / "paper.db"))
    broker = PersistentPaperBroker(repository, account_id="paper-a")
    _fill(broker, client_order_id="buy-full", side="BUY", price=10.2)
    _fill(broker, client_order_id="sell-full", side="SELL", price=9.8)
    _fill(broker, client_order_id="buy-partial", side="BUY", filled=50, price=10.1)

    report = calibrate_paper_account(
        repository, "paper-a", min_observations=3, conservative_quantile=0.75,
    )

    assert report.n_orders == 3
    assert report.n_filled_orders == 2
    assert report.n_partially_filled_orders == 1
    assert report.quantity_fill_rate == pytest.approx(250 / 300)
    assert report.n_price_observations == 3
    assert report.median_adverse_slippage == pytest.approx(0.02)
    assert report.conservative_adverse_slippage == pytest.approx(0.0202040816)
    assert report.worst_adverse_slippage == pytest.approx(10 / 9.8 - 1)
    assert report.recommended_slippage_rate == report.conservative_adverse_slippage
    assert report.ready is True
    assert report.to_dict()["ready"] is True


def test_paper_calibration_never_recommends_costs_without_enough_observations(tmp_path):
    repository = OrderRepository(Database(tmp_path / "paper.db"))
    broker = PersistentPaperBroker(repository, account_id="paper-a")
    _fill(broker, client_order_id="buy", side="BUY", price=10.2)

    report = calibrate_paper_account(repository, "paper-a", min_observations=2)

    assert report.n_price_observations == 1
    assert report.conservative_adverse_slippage == pytest.approx(0.02)
    assert report.recommended_slippage_rate is None
    assert report.ready is False


@pytest.mark.parametrize("kwargs", [
    {"min_observations": 0},
    {"conservative_quantile": 0.0},
    {"conservative_quantile": 1.1},
])
def test_paper_calibration_validates_parameters(kwargs):
    with pytest.raises(ValueError):
        calibrate_paper_execution([], **kwargs)
