from __future__ import annotations

import pandas as pd
import pytest

from quart.data.exposure_store import PITExposureStore
from quart.risk.exposure import ExposureDataError


def _history() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "as_of": "2024-01-02", "available_at": "2024-01-02", "symbol": "A",
            "benchmark_weight": 0.6, "industry": "科技", "market_cap": 10.0,
            "momentum": 0.2, "source": "csindex", "version": "v1",
        },
        {
            "as_of": "2024-01-02", "available_at": "2024-01-02", "symbol": "B",
            "benchmark_weight": 0.4, "industry": "银行", "market_cap": 20.0,
            "momentum": -0.2, "source": "csindex", "version": "v1",
        },
        {
            "as_of": "2024-02-01", "available_at": "2024-02-02", "symbol": "C",
            "benchmark_weight": 0.7, "industry": "医药", "market_cap": 30.0,
            "momentum": 0.1, "source": "csindex", "version": "v2",
        },
        {
            "as_of": "2024-02-01", "available_at": "2024-02-02", "symbol": "D",
            "benchmark_weight": 0.3, "industry": "消费", "market_cap": 40.0,
            "momentum": -0.1, "source": "csindex", "version": "v2",
        },
    ])


def test_store_uses_latest_snapshot_available_on_decision_date():
    store = PITExposureStore(_history())

    old = store.snapshot_at("2024-01-15")
    new = store.snapshot_at("2024-02-03")

    assert old.version == "v1"
    assert set(old.benchmark_weights.index) == {"A", "B"}
    assert new.version == "v2"
    assert set(new.benchmark_weights.index) == {"C", "D"}
    assert new.style_exposures is not None


def test_store_does_not_use_not_yet_available_snapshot():
    store = PITExposureStore(_history())

    snapshot = store.snapshot_at("2024-02-01")

    assert snapshot.version == "v1"


def test_store_rejects_ambiguous_latest_snapshot_version():
    history = _history()
    ambiguous = history[history["version"] == "v2"].copy()
    ambiguous["version"] = "other"
    with pytest.raises(ExposureDataError, match="多个 source/version"):
        PITExposureStore(pd.concat([history, ambiguous], ignore_index=True)).snapshot_at("2024-02-03")
