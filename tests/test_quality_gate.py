"""QUALITY-002：统一质量 Preflight。"""

from __future__ import annotations

import pandas as pd
import pytest

from quart.data.quality_gate import (
    DataQualityError,
    evaluate_quality_gate,
    load_quality_gate,
    require_quality_gate,
)


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02", "2024-01-03"]),
            "symbol": ["000001", "000001", "600000", "600000"],
            "open": [10.0, 10.1, 8.0, 8.1],
            "high": [10.2, 10.3, 8.2, 8.3],
            "low": [9.9, 10.0, 7.9, 8.0],
            "close": [10.1, 10.2, 8.1, 8.2],
            "volume": [100.0, 110.0, 120.0, 130.0],
            "amount": [101000.0, 112200.0, 97200.0, 106600.0],
        }
    )


def _benchmark() -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"]), "close": [100.0, 101.0]})


def test_quality_gate_passes_valid_input():
    result = evaluate_quality_gate(_bars(), _benchmark(), as_of="2024-01-03", blocked_symbols=set())
    assert result.passed, result.to_dict()
    assert result.metadata["symbols"] == 2


def test_quality_gate_catches_duplicate_ohlc_and_benchmark_coverage():
    bars = pd.concat([_bars(), _bars().iloc[[0]]], ignore_index=True)
    bars.loc[0, "high"] = 9.0
    benchmark = _benchmark().iloc[[0]]
    result = evaluate_quality_gate(bars, benchmark, as_of="2024-01-03", blocked_symbols=set())
    rule_ids = {issue.rule_id for issue in result.issues}
    assert {"QG-010", "QG-012", "QG-042"} <= rule_ids


def test_quality_gate_blocks_governed_symbol_and_persists_status(tmp_path):
    status_path = tmp_path / "meta" / "last_quality_gate.json"
    with pytest.raises(DataQualityError, match="QG-030"):
        require_quality_gate(
            _bars(),
            _benchmark(),
            as_of="2024-01-03",
            blocked_symbols={"600000"},
            status_path=status_path,
        )
    saved = load_quality_gate(status_path)
    assert saved is not None and saved["passed"] is False
    assert saved["issues"][0]["rule_id"] == "QG-030"


def test_quality_gate_rejects_future_data_at_earlier_as_of():
    result = evaluate_quality_gate(_bars(), _benchmark(), as_of="2024-01-02", blocked_symbols=set())
    assert any(issue.rule_id == "QG-004" for issue in result.issues)
