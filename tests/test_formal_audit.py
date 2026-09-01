"""正式研究审计可复现契约测试（RESEARCH-001）。

只覆盖纯逻辑：数据溯源（含 DATA-001 快照引用）、因子审计引用、
报告渲染与门禁集成。重回测（成本压力/WFA）由 admission 既有测试
与手动运行覆盖，不在单测中执行。
"""
from __future__ import annotations

import pandas as pd

from quart.data import snapshot as snap
from quart.data.artifacts import ArtifactStore, data_version
from quart.research.admission import GateResult, evaluate_gates
from quart.research.formal_audit import (
    _wfa_cmd,
    data_provenance,
    latest_factor_audit_ref,
    render_formal_report,
)


class _FakeStore:
    """最小 BarStore 替身：symbols 为空即走早退分支。"""

    def symbols(self) -> list[str]:
        return []


def _manifest(dataset: str, snapshot_id: str, master: str | None = None) -> snap.SnapshotManifest:
    return snap.SnapshotManifest(
        snapshot_id=snapshot_id,
        dataset_name=dataset,
        schema_version=1,
        created_at="2026-08-31T00:00:00+00:00",
        source="test",
        quality_status="scanned",
        security_master_version=master,
    )


# ---------------- 数据溯源 ----------------


def test_data_version_has_snapshot_keys_without_manifests(tmp_path):
    dv = data_version(_FakeStore(), snapshot_base=tmp_path)
    assert dv["symbols"] == 0
    assert dv["snapshot_ids"] == {"daily": None, "index": None}
    assert dv["security_master_version"] is None


def test_data_version_reads_snapshot_ids_from_base(tmp_path):
    snap.save_manifest(_manifest("daily", "snap-daily-1", master="master-v9"), base=tmp_path)
    snap.save_manifest(_manifest("index", "snap-index-1"), base=tmp_path)

    dv = data_version(_FakeStore(), snapshot_base=tmp_path)
    assert dv["snapshot_ids"] == {"daily": "snap-daily-1", "index": "snap-index-1"}
    assert dv["security_master_version"] == "master-v9"


def test_data_version_missing_dataset_degrades_to_none(tmp_path):
    snap.save_manifest(_manifest("daily", "snap-daily-2"), base=tmp_path)
    dv = data_version(_FakeStore(), snapshot_base=tmp_path)
    assert dv["snapshot_ids"]["daily"] == "snap-daily-2"
    assert dv["snapshot_ids"]["index"] is None


def test_data_provenance_structure(tmp_path):
    prov = data_provenance(_FakeStore(), snapshot_base=tmp_path)
    assert set(prov) == {"data_version", "code"}
    assert isinstance(prov["code"], str) and prov["code"]


# ---------------- 因子审计引用 ----------------


def test_latest_factor_audit_ref_empty_store(tmp_path):
    assert latest_factor_audit_ref(ArtifactStore(tmp_path)) is None


def test_latest_factor_audit_ref_reads_capacity_proxy(tmp_path):
    store = ArtifactStore(tmp_path)
    run = store.create_run("factor_audit", {"sample": "monthly"}, with_data_version=False)
    run.put_table(
        "provisional_baseline",
        pd.DataFrame([
            {"factor": "turn20_neg", "annual_return": 0.17,
             "max_drawdown": -0.2, "annual_turnover": 6.5,
             "top_amount_median_m": 320.0, "capacity_proxy_m": 32.0},
        ]),
    )
    run.finish()

    ref = latest_factor_audit_ref(store)
    assert ref is not None
    assert ref["run_id"] == run.manifest.run_id
    assert ref["fingerprint"] == run.manifest.fingerprint
    assert ref["capacity_proxy"][0]["factor"] == "turn20_neg"
    assert ref["capacity_proxy"][0]["capacity_proxy_m"] == 32.0


# ---------------- 报告渲染 ----------------


def _canned_inputs(snapshot_id: str | None = "snap-daily-1") -> dict:
    provenance = {
        "data_version": {
            "symbols": 3200, "first_date": "2024-01-02", "last_date": "2026-08-28",
            "snapshot_ids": {"daily": snapshot_id, "index": None},
            "security_master_version": "master-v9",
        },
        "code": "abc1234",
    }
    cost = {
        0.0: {"cagr": 0.20, "sharpe": 1.2, "max_drawdown": -0.15,
              "bench_excess_cagr": 0.12, "n_trades": 200},
        1.0: {"cagr": 0.10, "sharpe": 0.8, "max_drawdown": -0.20,
              "bench_excess_cagr": 0.05, "n_trades": 200},
        2.0: {"cagr": 0.02, "sharpe": 0.4, "max_drawdown": -0.25,
              "bench_excess_cagr": 0.01, "n_trades": 200},
    }
    return {"provenance": provenance, "cost": cost}


def test_render_report_pass_and_fail_verdicts():
    inputs = _canned_inputs()
    gates_pass = evaluate_gates(
        inputs["cost"],
        {"cagr": 0.08, "max_drawdown": -0.22},
        {"sharpe_1x_min": 0.5, "cagr_2x_min": 0.0},
    )
    md = render_formal_report(
        strategy="lowvol_indz", start="2024-03-01", end=None,
        oos_start="2025-09-01", provenance=inputs["provenance"],
        cost_summaries=inputs["cost"],
        oos_summary={"cagr": 0.06, "sharpe": 0.7, "max_drawdown": -0.18,
                     "bench_excess_cagr": 0.03, "n_trades": 60},
        wfa_summary={"cagr": 0.08, "sharpe": 0.9, "max_drawdown": -0.22},
        gate_result=gates_pass, factor_ref=None,
        run_id="research_audit_x", fingerprint="fp123",
    )
    assert gates_pass.passed is True
    assert "# 正式研究审计报告：lowvol_indz" in md
    assert "`snap-daily-1`" in md  # 数据溯源钉住快照
    assert "成本压力" in md and "0x" in md and "2x" in md
    assert "纯 OOS 冻结验证" in md and "WFA 样本外" in md
    assert "容量与因子审计引用" in md
    assert "PASS" in md and "FAIL —— 禁止晋级实盘" not in md

    gates_fail: GateResult = evaluate_gates({}, None)
    md_fail = render_formal_report(
        strategy="x", start="2024-01-01", end=None, oos_start=None,
        provenance=inputs["provenance"], cost_summaries={},
        oos_summary=None, wfa_summary=None, gate_result=gates_fail,
        factor_ref=None, run_id="r", fingerprint="f",
    )
    assert gates_fail.passed is False
    assert "FAIL" in md_fail


def test_render_report_warns_without_snapshot():
    inputs = _canned_inputs(snapshot_id=None)
    gates = evaluate_gates(inputs["cost"], {"cagr": 0.08, "max_drawdown": -0.2})
    md = render_formal_report(
        strategy="x", start="2024-01-01", end=None, oos_start=None,
        provenance=inputs["provenance"], cost_summaries=inputs["cost"],
        oos_summary=None, wfa_summary=None, gate_result=gates,
        factor_ref=None, run_id="r", fingerprint="f",
    )
    assert "快照未构建" in md


def test_render_report_includes_factor_ref_capacity_table():
    inputs = _canned_inputs()
    gates = evaluate_gates(inputs["cost"], {"cagr": 0.08, "max_drawdown": -0.2})
    ref = {
        "run_id": "factor_audit_1", "fingerprint": "fa-fp", "created_at": "now",
        "data_version": {}, "params": {},
        "capacity_proxy": [
            {"factor": "turn20_neg", "annual_return": 0.1739,
             "annual_turnover": 6.5, "top_amount_median_m": 320.0,
             "capacity_proxy_m": 32.0},
        ],
    }
    md = render_formal_report(
        strategy="x", start="2024-01-01", end=None, oos_start=None,
        provenance=inputs["provenance"], cost_summaries=inputs["cost"],
        oos_summary=None, wfa_summary=None, gate_result=gates,
        factor_ref=ref, run_id="r", fingerprint="f",
    )
    assert "`factor_audit_1`" in md and "turn20_neg" in md
    assert "流动性代理" in md


# ---------------- WFA 命令构造 ----------------


def test_wfa_cmd_baseline():
    cmd = _wfa_cmd("lowvol_indz", "2024-03-01")
    assert cmd[1:] == ["scripts/walk_forward.py", "--strategy", "lowvol_indz",
                       "--start", "2024-03-01"]
    assert "--grid" not in cmd


def test_wfa_cmd_freezes_candidate_params_as_single_value_grids():
    cmd = _wfa_cmd("lowvol_indz", "2024-03-01", end="2026-08-31",
                   params={"size_weight": 0.3, "top_k": 40})
    assert cmd[cmd.index("--end"): cmd.index("--end") + 2] == ["--end", "2026-08-31"]
    grids = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--grid"]
    assert grids == ["size_weight=0.3", "top_k=40"]
