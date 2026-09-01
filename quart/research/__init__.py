"""Reusable quantitative research primitives."""

from quart.research.factor_audit import (
    FACTOR_SPECS,
    FactorAuditResult,
    FactorSpec,
    rank_correlation,
    run_factor_audit,
)
from quart.research.event_factors import (
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
    market_limit_sentiment,
    neutralize_against,
    price_limit_panel,
)
from quart.research.formal_audit import (
    COST_MULTIPLIERS,
    data_provenance,
    latest_factor_audit_ref,
    render_formal_report,
    run_cost_stress,
    run_single_backtest,
    run_wfa_subprocess,
)

__all__ = [
    "COST_MULTIPLIERS",
    "FACTOR_SPECS",
    "FactorAuditResult",
    "FactorSpec",
    "data_provenance",
    "dragon_tiger_panels",
    "event_sentiment_panels",
    "latest_factor_audit_ref",
    "limit_event_panels",
    "market_limit_sentiment",
    "neutralize_against",
    "price_limit_panel",
    "rank_correlation",
    "render_formal_report",
    "run_cost_stress",
    "run_factor_audit",
    "run_single_backtest",
    "run_wfa_subprocess",
]
