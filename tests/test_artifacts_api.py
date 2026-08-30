"""制品 API 测试。

验证前端能按 run_id 追溯到"这个数字是哪次运行、用哪套参数、
跑在哪份数据上产生的"——这是旧 reports/ 机制做不到的。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from api import artifacts_api
from quart.data.artifacts import STATUS_FAILED, ArtifactStore


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """构造若干次运行，并把仓库根目录指向 tmp_path。"""
    monkeypatch.setattr(artifacts_api, "_store", lambda: ArtifactStore(root=tmp_path / "af"))
    store = ArtifactStore(root=tmp_path / "af")

    r1 = store.create_run("backtest_lowvol_indz", {"top_k": 20}, with_data_version=False)
    r1.put_table("equity", pd.DataFrame({"equity": [1.0, 1.1]}))
    r1.put_json("summary", {"cagr": 0.07})
    r1.add_metrics(cagr=0.07, sharpe=0.67, n_trades=120)
    m1 = r1.finish()

    r2 = store.create_run("backtest_lowvol_indz", {"top_k": 30}, with_data_version=False)
    r2.add_metrics(cagr=0.05, sharpe=0.4, n_trades=200)
    m2 = r2.finish()

    r3 = store.create_run("wfa_lowvol_indz", {"train": 504}, with_data_version=False)
    r3.put_table("folds", pd.DataFrame({"fold": [0, 1], "oos_cagr": [0.08, -0.02]}))
    r3.add_metrics(decay=0.62, n_folds=6, oos_cagr=0.031,
                   param_stability={"top_k": 0.83})
    m3 = r3.finish()

    r4 = store.create_run("backtest_momentum", with_data_version=False)
    r4.finish(status=STATUS_FAILED, error="数据已过期 9 天")

    return {"store": store, "m1": m1, "m2": m2, "m3": m3}


def test_list_runs_returns_frame(seeded):
    df = artifacts_api.list_runs()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4
    assert {"run_id", "task", "created_at", "status", "fingerprint"} <= set(df.columns)
    # 最新在前
    assert df["created_at"].is_monotonic_decreasing


def test_list_runs_filters_by_task(seeded):
    df = artifacts_api.list_runs(task="backtest_lowvol_indz")
    assert len(df) == 2


def test_metrics_are_flattened_into_columns(seeded):
    df = artifacts_api.list_runs(task="backtest_lowvol_indz")
    assert "m_cagr" in df.columns
    assert "m_sharpe" in df.columns
    assert set(df["m_cagr"].tolist()) == {0.07, 0.05}


def test_nested_metrics_excluded(seeded):
    """param_stability 是 dict，不能塞进表格列。"""
    df = artifacts_api.list_runs(task="wfa_lowvol_indz")
    assert "m_decay" in df.columns
    assert "m_param_stability" not in df.columns


def test_get_run_returns_full_context(seeded):
    d = artifacts_api.get_run(seeded["m1"].run_id)
    assert d is not None
    assert d["params"] == {"top_k": 20}
    assert d["metrics"]["cagr"] == 0.07
    assert [a["name"] for a in d["artifacts"]] == ["equity", "summary"]


def test_get_run_rejects_traversal(seeded):
    assert artifacts_api.get_run("../../etc/passwd") is None
    assert artifacts_api.get_run("") is None
    assert artifacts_api.get_run("a/b") is None


def test_latest_run_prefers_newest_ok(seeded):
    d = artifacts_api.latest_run("backtest_lowvol_indz")
    assert d["params"]["top_k"] == 30, "应返回最新一次成功运行"


def test_read_table_and_json(seeded):
    m1 = seeded["m1"]
    df = artifacts_api.read_table(m1.run_id, "equity")
    assert df["equity"].tolist() == [1.0, 1.1]
    assert artifacts_api.read_json(m1.run_id, "summary") == {"cagr": 0.07}
    assert artifacts_api.read_text(m1.run_id, "equity") is None


def test_backtest_runs_view(seeded):
    df = artifacts_api.backtest_runs()
    assert not df.empty
    # 应含 backtest_* 与 wfa_*，排除 signal_*
    assert all(str(t).startswith(("backtest_", "wfa_")) for t in df["task"])


def test_latest_wfa_exposes_overfit_diagnostics(seeded):
    d = artifacts_api.latest_wfa()
    assert d is not None
    assert d["decay"] == 0.62
    assert d["n_folds"] == 6
    assert d["param_stability"] == {"top_k": 0.83}


def test_latest_wfa_filters_by_task(seeded):
    assert artifacts_api.latest_wfa("wfa_lowvol_indz") is not None
    assert artifacts_api.latest_wfa("wfa_momentum") is None


def test_failed_runs_are_visible(seeded):
    """失败的运行必须能被查到——此前只在日志里，前端完全看不到。"""
    df = artifacts_api.failed_runs()
    assert len(df) == 1
    assert "数据已过期" in df.iloc[0]["error"]


def test_failed_run_excluded_from_latest(seeded):
    assert artifacts_api.latest_run("backtest_momentum") is None


def test_prune_keeps_recent(seeded):
    removed = artifacts_api.prune(keep_last=2)
    assert removed == 2
    assert len(artifacts_api.list_runs()) == 2


def test_artifact_path_stays_inside_store(seeded):
    store = seeded["store"]
    p = artifacts_api.artifact_path(seeded["m1"].run_id, "equity")
    assert store.root in p.parents
    assert artifacts_api.artifact_path("../x", "equity") is None


def test_manifest_is_human_readable_json(seeded):
    """manifest 应能直接给人看——出问题时靠它排查。"""
    path = seeded["store"].root / seeded["m1"].run_id / "manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["task"] == "backtest_lowvol_indz"
    assert raw["status"] == "ok"
    assert "fingerprint" in raw
    assert raw["artifacts"][0]["rows"] == 2
