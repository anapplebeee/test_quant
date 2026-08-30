from quart.execution.models import BUY
from quart.manual_trading import PlannedOrderInput, TradingRepository


def _patch_api(monkeypatch, tmp_path):
    import api.manual_trading_api as api

    repo = TradingRepository(tmp_path / "trading.db")
    repo.initialize_schema()
    monkeypatch.setattr(api, "repository", lambda: repo)
    monkeypatch.setattr(api, "manual_settings", lambda: (tmp_path / "trading.db", "manual"))
    monkeypatch.setattr(api, "_latest_prices", lambda symbols: {symbol: 10.0 for symbol in symbols})
    return api, repo


def test_frontend_account_plan_fill_and_review(monkeypatch, tmp_path):
    api, repo = _patch_api(monkeypatch, tmp_path)
    status, summary, positions = api.initialize_account_action(
        "2026-08-28",
        100_000,
        '{"600001": {"total_quantity": 100, "sellable_quantity": 100, "cost_price": 9.0}}',
    )
    assert status.startswith("✅")
    assert "100,000.00" in summary
    assert positions.iloc[0]["代码"] == "600001"

    state = repo.account_state(as_of="2026-08-28")
    plan_id = repo.create_trade_plan(
        account_id=state.account_id,
        strategy_name="lowvol_indz",
        signal_date="2026-08-28",
        intended_trade_date="2026-08-31",
        orders=[PlannedOrderInput("600000", BUY, 100, 10.0)],
    )
    plans, choices = api.plans_view(as_of="2026-08-31")
    assert plans.iloc[0]["计划ID"] == plan_id
    assert api.approve_plan_action(choices[0]).startswith("✅")

    fill_status = api.record_fill_action(
        "2026-08-31",
        "09:35:00",
        "600000",
        "BUY",
        100,
        10.1,
        None,
        "front-1",
        0,
        0,
        0,
        0,
        "2026-09-01",
        True,
    )
    assert fill_status.startswith("✅")
    review, detail = api.execution_view(choices[0])
    assert "100.0%" in review
    assert detail.iloc[0]["成交数量"] == 100


def test_frontend_reconciliation_preview_does_not_mutate(monkeypatch, tmp_path):
    api, repo = _patch_api(monkeypatch, tmp_path)
    repo.initialize_account(cash=100_000, positions={}, as_of="2026-08-28")
    payload = api.reconciliation_template("2026-08-31")
    message, differences = api.reconcile_action(payload, confirm=False)
    assert "仅预览" in message
    assert not differences.empty
    state = repo.account_state(as_of="2026-08-31")
    assert state.total_positions == {}
