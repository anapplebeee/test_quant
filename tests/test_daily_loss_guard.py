"""RISK-002：日初权益基线、日损熔断和每日信号前置守卫。"""
from __future__ import annotations

import pytest

from quart.infrastructure.db import Database
from quart.pipeline import _apply_daily_loss_guard
from quart.risk import DailyLossGuard, RiskLimits, RiskRepository, RiskState


@pytest.fixture()
def risk_repo(tmp_path):
    return RiskRepository(Database(tmp_path / "risk.db"))


def _guard(repo: RiskRepository) -> DailyLossGuard:
    return DailyLossGuard(
        RiskLimits(max_position_pct=0.25, max_daily_loss_pct=0.05),
        repo,
    )


def test_first_observation_is_explicit_baseline_not_silent_zero_loss(risk_repo):
    assessment = _guard(risk_repo).evaluate("manual", "2026-09-01", 100_000)

    assert not assessment.mark.baseline_available
    assert assessment.mark.daily_loss_pct == 0.0
    assert not assessment.triggered
    stored = risk_repo.get_daily_mark("manual", assessment.mark.trade_date)
    assert stored is not None and stored.current_equity == 100_000


def test_daily_loss_uses_prior_day_equity_and_halts_account(risk_repo):
    guard = _guard(risk_repo)
    guard.evaluate("manual", "2026-09-01", 100_000)

    assessment = guard.evaluate("manual", "2026-09-02", 94_000)

    assert assessment.mark.baseline_available
    assert assessment.mark.opening_equity == 100_000
    assert assessment.mark.daily_loss_pct == pytest.approx(0.06)
    assert assessment.triggered
    assert assessment.state_before is RiskState.ACTIVE
    assert assessment.state_after is RiskState.HALTED
    assert risk_repo.get_state("manual") is RiskState.HALTED
    assert "日损 6.00%" in assessment.reason
    history = risk_repo.state_history("manual")[0]
    assert "日损 6.00%" in history["reason"]
    assert history["limit_version"] == assessment.mark.limit_version


def test_same_day_rerun_preserves_opening_equity_and_is_idempotent(risk_repo):
    guard = _guard(risk_repo)
    first = guard.evaluate(
        "manual", "2026-09-01", 98_000, opening_equity=100_000
    )
    second = guard.evaluate("manual", "2026-09-01", 94_000)

    assert first.mark.opening_equity == second.mark.opening_equity == 100_000
    assert second.mark.daily_loss_pct == pytest.approx(0.06)
    assert second.triggered
    assert len(risk_repo.state_history("manual")) == 1


@pytest.mark.parametrize("equity", [-1.0, 0.0])
def test_signal_guard_fails_closed_on_invalid_equity(risk_repo, equity):
    warnings: list[str] = []
    state, assessment = _apply_daily_loss_guard(
        "manual",
        "2026-09-01",
        equity,
        RiskLimits(max_position_pct=0.25),
        warnings,
        repository=risk_repo,
    )

    assert state is RiskState.HALTED
    assert assessment is None
    assert "fail-closed" in warnings[0]


def test_signal_guard_exposes_trigger_to_order_generation_path(risk_repo):
    guard = _guard(risk_repo)
    guard.evaluate("manual", "2026-09-01", 100_000)
    warnings: list[str] = []

    state, assessment = _apply_daily_loss_guard(
        "manual",
        "2026-09-02",
        94_000,
        RiskLimits(max_position_pct=0.25, max_daily_loss_pct=0.05),
        warnings,
        repository=risk_repo,
    )

    assert state is RiskState.HALTED
    assert assessment is not None and assessment.triggered
    assert any("日损熔断" in warning for warning in warnings)
