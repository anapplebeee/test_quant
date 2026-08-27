from __future__ import annotations

import pandas as pd

from quart.strategy.industries import industry_neutralize


def test_neutralize_subtracts_group_means():
    scores = pd.Series({"A": 0.10, "B": 0.20, "C": 0.30, "D": -0.30})
    industries = pd.Series({"A": "医药", "B": "医药", "C": "银行", "D": "银行"})
    industries.index.name = None
    out = industry_neutralize(scores, industries, min_group_size=2)

    assert abs(out["A"] - (-0.05)) < 1e-12
    assert abs(out["B"] - (+0.05)) < 1e-12
    assert abs(out["C"] - (+0.30)) < 1e-12
    assert abs(out["D"] - (-0.30)) < 1e-12


def test_small_groups_keep_raw_score():
    scores = pd.Series({"A": 1.0, "B": -1.0})
    industries = pd.Series({"A": "医药"})
    out = industry_neutralize(scores, industries, min_group_size=2)
    assert out["A"] == 1.0
    assert out["B"] == -1.0


def test_all_same_industry_preserves_relative_scores():
    scores = pd.Series({"A": 0.5, "B": -0.2, "C": 0.7})
    industries = pd.Series({"A": "T", "B": "T", "C": "T"})
    out = industry_neutralize(scores, industries, min_group_size=3)
    assert abs(out.max() - out.min() - (scores.max() - scores.min())) < 1e-9
