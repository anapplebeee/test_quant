from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.portfolio import (
    PortfolioConstraints,
    PortfolioConstructionInput,
    PortfolioConstructor,
    PortfolioInfeasibleError,
)


def _request(**overrides) -> PortfolioConstructionInput:
    values = {
        "alphas": pd.Series({"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0}),
        "current_weights": {"A": 0.225, "B": 0.225, "C": 0.225, "D": 0.225},
        "benchmark_weights": {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25},
        "equity": 1_000_000.0,
        "tradable": {"A", "B", "C", "D"},
        "industries": {"A": "科技", "B": "科技", "C": "银行", "D": "银行"},
        "market_caps": {"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0},
        "style_exposures": pd.DataFrame(
            {"momentum": {"A": 0.2, "B": 0.1, "C": -0.1, "D": -0.2}},
        ),
        "covariance": pd.DataFrame(np.eye(4), index=list("ABCD"), columns=list("ABCD")),
        "adv": {"A": 3_000_000.0, "B": 3_000_000.0, "C": 3_000_000.0, "D": 3_000_000.0},
        "transaction_cost_bps": 10.0,
        "risk_aversion": 0.1,
        "turnover_penalty": 1.0,
    }
    values.update(overrides)
    return PortfolioConstructionInput(**values)


def test_constructor_enforces_and_audits_all_v1_constraints():
    constraints = PortfolioConstraints(
        max_weight=0.30,
        min_cash_weight=0.10,
        max_turnover=0.30,
        max_adv_participation=0.10,
        industry_active_bounds=0.25,
        market_cap_active_bound=0.50,
        style_active_bounds={"momentum": 0.10},
    )
    constructor = PortfolioConstructor()

    result = constructor.construct(_request(), constraints)
    again = constructor.construct(_request(), constraints)

    pd.testing.assert_series_equal(result.target_weights, again.target_weights)
    assert result.target_weights.sum() == pytest.approx(0.90)
    assert (result.target_weights <= 0.30 + 1e-10).all()
    assert result.cash_weight == pytest.approx(0.10)
    assert result.expected_turnover <= 0.30 + 1e-10
    assert result.expected_variance is not None
    assert result.expected_cost > 0
    assert {"cash.minimum", "turnover", "market_cap.active", "style.momentum"} <= set(
        result.constraint_usage
    )
    assert {"industry.科技", "industry.银行", "adv.A", "position.A"} <= set(result.constraint_usage)
    assert all(item.headroom >= -1e-9 for item in result.constraint_usage.values())


def test_constructor_freezes_untradable_positions():
    result = PortfolioConstructor().construct(
        PortfolioConstructionInput(
            alphas=pd.Series({"A": 10.0, "B": 1.0}),
            current_weights={"A": 0.20, "B": 0.20},
            tradable={"B"},
        ),
        PortfolioConstraints(max_weight=0.60, min_cash_weight=0.20),
    )

    assert result.target_weights["A"] == pytest.approx(0.20)
    assert result.constraint_usage["frozen.A"].used == 0.0
    assert result.target_weights["B"] == pytest.approx(0.60)


def test_constructor_clips_to_adv_and_turnover_limits():
    result = PortfolioConstructor().construct(
        PortfolioConstructionInput(
            alphas=pd.Series({"A": 0.0, "B": 1.0}),
            current_weights={"A": 0.50, "B": 0.50},
            equity=1_000_000.0,
            adv={"A": 1_000_000.0, "B": 1_000_000.0},
        ),
        PortfolioConstraints(
            max_weight=1.0,
            max_turnover=0.10,
            max_adv_participation=0.10,
        ),
    )

    assert result.target_weights["A"] == pytest.approx(0.40)
    assert result.target_weights["B"] == pytest.approx(0.60)
    assert result.expected_turnover == pytest.approx(0.10)
    assert result.constraint_usage["adv.A"].used == pytest.approx(100_000.0)


def test_constructor_fails_closed_when_style_constraint_has_no_feasible_solution():
    request = PortfolioConstructionInput(
        alphas=pd.Series({"A": 1.0}),
        current_weights={"B": 0.5},
        benchmark_weights={"A": 0.5, "B": 0.5},
        tradable=set(),
        style_exposures=pd.DataFrame({"growth": {"A": 1.0, "B": -1.0}}),
    )
    constraints = PortfolioConstraints(
        max_weight=0.5,
        min_cash_weight=0.0,
        style_active_bounds={"growth": 0.0},
    )

    with pytest.raises(PortfolioInfeasibleError, match="风格主动暴露超限"):
        PortfolioConstructor().construct(request, constraints)


def test_constructor_rejects_missing_required_constraint_data():
    with pytest.raises(PortfolioInfeasibleError, match="market_caps"):
        PortfolioConstructor().construct(
            PortfolioConstructionInput(alphas=pd.Series({"A": 1.0})),
            PortfolioConstraints(max_weight=1.0, market_cap_active_bound=0.1),
        )


def test_constructor_rejects_invalid_weight_input_without_clipping():
    with pytest.raises(PortfolioInfeasibleError, match="current_weights"):
        PortfolioConstructor().construct(
            PortfolioConstructionInput(
                alphas=pd.Series({"A": 1.0}),
                current_weights={"A": -0.1},
            ),
            PortfolioConstraints(max_weight=1.0),
        )


def test_constructor_tolerates_frozen_overweight_position():
    """停牌/无行情持仓权重被动超单票上限 → 不崩溃，冻结保留（RESEARCH 600803 场景）。"""
    result = PortfolioConstructor().construct(
        PortfolioConstructionInput(
            alphas=pd.Series({"A": 0.5, "B": 1.0}),
            current_weights={"A": 0.30, "B": 0.05},
            tradable={"B"},
        ),
        PortfolioConstraints(max_weight=0.10, min_cash_weight=0.0),
    )
    # A 停牌冻结、权重 30% > 10% 上限：保留而非报错
    assert result.target_weights["A"] == pytest.approx(0.30)
    assert result.constraint_usage["frozen.A"].used == pytest.approx(0.0)
    assert result.target_weights["B"] == pytest.approx(0.10)
