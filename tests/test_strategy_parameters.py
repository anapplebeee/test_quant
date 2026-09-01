"""策略参数 schema、前端透传与因子执行回执。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from api.strategy_api import (
    default_strategy_name,
    encode_strategy_parameter_table,
    strategy_parameter_table,
)
from api.task_api import validate_extra_args
from quart.strategy.parameters import (
    build_factor_receipt,
    core_strategy_overrides,
    parse_strategy_assignments,
)


def test_configured_default_strategy_is_used():
    assert default_strategy_name() == "lowvol_indz"


def test_dynamic_parameter_table_tracks_each_strategy_schema():
    lowvol = strategy_parameter_table("lowvol_indz")
    keys = set(lowvol["参数"])
    assert {"vg_weight", "event_crowding_weight", "candidate_quality_weight"} <= keys
    assert "top_k" not in keys
    assert "rebalance_days" not in keys

    dual_ma = strategy_parameter_table("dual_ma")
    assert {"fast_days", "slow_days", "max_weight_pct"} <= set(dual_ma["参数"])
    assert "max_names" not in set(dual_ma["参数"])


def test_parameter_table_round_trips_typed_values():
    table = strategy_parameter_table("lowvol_indz")
    table.loc[table["参数"] == "vg_weight", "值"] = "0.35"
    table.loc[table["参数"] == "event_crowding_only", "值"] = "true"
    assignments = encode_strategy_parameter_table("lowvol_indz", table)
    parsed = parse_strategy_assignments("lowvol_indz", assignments)
    assert parsed["vg_weight"] == pytest.approx(0.35)
    assert parsed["event_crowding_only"] is True
    assert parsed["industry_z"] is True


def test_parameter_table_rejects_unknown_or_out_of_range_values():
    table = pd.DataFrame([{"参数": "not_a_factor", "值": "1"}])
    with pytest.raises(ValueError, match="不支持参数"):
        encode_strategy_parameter_table("lowvol_indz", table)

    table = pd.DataFrame([{"参数": "vg_weight", "值": "1.5"}])
    with pytest.raises(ValueError, match="0 到 1"):
        encode_strategy_parameter_table("lowvol_indz", table)


def test_core_top_k_maps_to_strategy_specific_key():
    assert core_strategy_overrides("lowvol_indz", top_k=30) == {"top_k": 30}
    assert core_strategy_overrides("dual_ma", top_k=12) == {"max_names": 12}


def test_task_api_allows_safe_schema_param_and_rejects_injection():
    ok, error = validate_extra_args(
        "backtest",
        ["--strategy", "lowvol_indz", "--param", "vg_weight=0.3"],
    )
    assert ok, error
    ok, _ = validate_extra_args(
        "backtest",
        ["--strategy", "lowvol_indz", "--param", "vg_weight=0.3;whoami"],
    )
    assert not ok


def test_factor_receipt_records_runtime_degradation():
    runtime = SimpleNamespace(vg_score=None)
    receipt = build_factor_receipt(
        "lowvol_indz",
        {"vg_weight": 0.3},
        strategy=runtime,
    )
    vg = next(item for item in receipt["enabled_factors"] if item["key"] == "vg_weight")
    assert receipt["is_factor_strategy"] is True
    assert vg["status"] == "degraded"
    assert receipt["degraded_count"] == 1
    assert receipt["warnings"]

    dual = build_factor_receipt("dual_ma")
    assert dual["is_factor_strategy"] is False
    assert "MA" in dual["formula"]


def test_backtest_api_prefers_persisted_factor_receipt(tmp_path, monkeypatch):
    import api.backtest_api as backtest_api

    receipt = build_factor_receipt("lowvol_indz", {"vg_weight": 0.3})
    (tmp_path / "summary_lowvol_indz_20260901_120000.json").write_text(
        json.dumps({"factor_receipt": receipt}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(backtest_api, "reports_dir", lambda: tmp_path)
    loaded = backtest_api.get_factor_execution_receipt("lowvol_indz_20260901_120000")
    assert loaded is not None
    assert loaded["formula"] == receipt["formula"]
    assert loaded["source"] == "run"
