from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from quart.risk.exposure import (
    ExposureDataError,
    ExposureLimits,
    ExposureSnapshot,
    parse_style_bounds,
)


def _snapshot(*, available_at: str = "2024-01-02") -> ExposureSnapshot:
    return ExposureSnapshot(
        as_of=pd.Timestamp("2024-01-02"),
        available_at=pd.Timestamp(available_at),
        benchmark_weights=pd.Series({"A": 0.5, "B": 0.5}),
        industries=pd.Series({"A": "科技", "B": "银行"}),
        market_caps=pd.Series({"A": 10.0, "B": 20.0}),
        style_exposures=pd.DataFrame({"momentum": {"A": 0.2, "B": -0.2}}),
        source="test_pit_feed",
        version="v1",
    )


def test_exposure_snapshot_resolves_complete_pit_inputs():
    inputs = _snapshot().resolve(
        "2024-01-03",
        ["A", "B"],
        ExposureLimits(
            industry_active_bounds=0.1,
            market_cap_active_bound=0.2,
            style_active_bounds={"momentum": 0.3},
        ),
    )

    assert inputs.benchmark_weights.sum() == pytest.approx(1.0)
    assert inputs.industries is not None and inputs.industries["A"] == "科技"
    assert inputs.market_caps is not None and inputs.market_caps["B"] == 20.0
    assert inputs.style_exposures is not None
    assert inputs.version == "v1"


def test_exposure_snapshot_rejects_forward_available_data():
    with pytest.raises(ExposureDataError, match="前视"):
        _snapshot(available_at="2024-01-04").resolve(
            "2024-01-03", ["A", "B"], ExposureLimits(market_cap_active_bound=0.2),
        )


def test_exposure_snapshot_rejects_missing_required_coverage():
    snapshot = replace(
        _snapshot(), benchmark_weights=pd.Series({"A": 0.5, "B": 0.4, "C": 0.1}),
    )
    with pytest.raises(ExposureDataError, match="market_caps"):
        snapshot.resolve(
            "2024-01-03", ["A", "B", "C"], ExposureLimits(market_cap_active_bound=0.2),
        )


def test_parse_style_bounds_is_deterministic_and_strict():
    assert parse_style_bounds("size=0.2,momentum=0.1") == {"momentum": 0.1, "size": 0.2}
    with pytest.raises(ValueError, match="不能重复"):
        parse_style_bounds("size=0.2,size=0.1")
