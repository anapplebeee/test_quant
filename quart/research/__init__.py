"""Reusable quantitative research primitives."""

from quart.research.event_factors import (
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
    market_limit_sentiment,
    neutralize_against,
    price_limit_panel,
)
from quart.research.factor_audit import (
    FACTOR_SPECS,
    FactorAuditResult,
    FactorSpec,
    rank_correlation,
    run_factor_audit,
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
from quart.research.limit_streak import (
    build_limit_streak_events,
    close_limit_hits,
    consecutive_true_counts,
    summarize_limit_streak_events,
    summarize_limit_streak_progression,
)

__all__ = [
    "COST_MULTIPLIERS",
    "FACTOR_SPECS",
    "FactorAuditResult",
    "FactorSpec",
    "build_limit_streak_events",
    "close_limit_hits",
    "consecutive_true_counts",
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
    "summarize_limit_streak_events",
    "summarize_limit_streak_progression",
]
