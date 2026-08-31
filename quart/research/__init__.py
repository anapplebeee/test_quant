"""Reusable quantitative research primitives."""

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

__all__ = [
    "COST_MULTIPLIERS",
    "FACTOR_SPECS",
    "FactorAuditResult",
    "FactorSpec",
    "data_provenance",
    "latest_factor_audit_ref",
    "rank_correlation",
    "render_formal_report",
    "run_cost_stress",
    "run_factor_audit",
    "run_single_backtest",
    "run_wfa_subprocess",
]
