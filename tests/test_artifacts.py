"""ArtifactStore 测试。

此前产出靠 `glob + mtime` 猜，无法回答"这个数字是哪次运行产生的"。
这里验证新契约能做到：按 run_id 追溯参数/数据版本/产出清单。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quart.data.artifacts import (
    STATUS_FAILED,
    STATUS_OK,
    ArtifactStore,
    fingerprint,
)


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


def test_create_run_writes_manifest(store):
    run = store.create_run("backtest", {"strategy": "lowvol_indz"},
                           with_data_version=False)
    m = run.finish()
    assert (store.root / m.run_id / "manifest.json").exists()
    assert m.status == STATUS_OK
    assert m.params == {"strategy": "lowvol_indz"}


def test_run_id_is_unique_per_run(store):
    a = store.create_run("backtest", with_data_version=False).finish()
    b = store.create_run("backtest", with_data_version=False).finish()
    assert a.run_id != b.run_id, "并发/快速连开时 run_id 必须唯一"


def test_put_and_read_table(store):
    run = store.create_run("backtest", with_data_version=False)
    df = pd.DataFrame({"equity": [1.0, 1.1, 1.2]})
    art = run.put_table("equity", df)
    m = run.finish()

    assert art.rows == 3
    assert art.kind == "table"
    back = store.read(m.run_id, "equity")
    assert back is not None
    assert back["equity"].tolist() == [1.0, 1.1, 1.2]


def test_put_json_and_text(store):
    run = store.create_run("signal", with_data_version=False)
    run.put_json("summary", {"cagr": 0.07, "中文": "值"})
    run.put_text("report", "# 报告\n内容")
    m = run.finish()

    raw = json.loads((store.root / m.run_id / "summary.json").read_text(encoding="utf-8"))
    assert raw["cagr"] == 0.07
    assert raw["中文"] == "值"
    assert store.read_text(m.run_id, "report") == "# 报告\n内容"


def test_add_metrics_lands_in_manifest(store):
    run = store.create_run("backtest", with_data_version=False)
    run.add_metrics(cagr=0.071, sharpe=0.67, max_drawdown=-0.226)
    m = run.finish()
    assert m.metrics["cagr"] == 0.071
    assert m.metrics["sharpe"] == 0.67


def test_metrics_handle_numpy_and_timestamps(store):
    """numpy 标量与 Timestamp 必须能 JSON 序列化（此前常炸在这里）。"""
    import numpy as np

    run = store.create_run("backtest", {
        "start": pd.Timestamp("2024-01-01"),
        "top_k": np.int64(20),
        "ratio": np.float64(0.15),
    }, with_data_version=False)
    run.add_metrics(best=np.float64(0.5), when=pd.Timestamp("2024-06-01"))
    m = run.finish()
    # 能完整读回 = 序列化没炸
    assert store.load_manifest(m.run_id).metrics["best"] == 0.5


def test_put_table_replaces_same_name(store):
    run = store.create_run("backtest", with_data_version=False)
    run.put_table("equity", pd.DataFrame({"v": [1]}))
    run.put_table("equity", pd.DataFrame({"v": [1, 2, 3]}))
    m = run.finish()
    assert [a for a in m.artifacts if a.name == "equity"][0].rows == 3
    assert len([a for a in m.artifacts if a.name == "equity"]) == 1


def test_failed_run_records_error(store):
    run = store.create_run("backtest", with_data_version=False)
    m = run.finish(status=STATUS_FAILED, error="data empty")
    assert m.status == STATUS_FAILED
    assert m.error == "data empty"


def test_list_runs_sorted_newest_first(store):
    for i in range(3):
        store.create_run("backtest", {"i": i}, with_data_version=False).finish()
    store.create_run("sweep", with_data_version=False).finish()

    assert len(store.list_runs()) == 4
    assert len(store.list_runs(task="backtest")) == 3
    assert len(store.list_runs(task="sweep")) == 1
    # 最新在前
    latest = store.latest("backtest")
    assert latest.params["i"] == 2


def test_latest_respects_status(store):
    run = store.create_run("backtest", with_data_version=False)
    run.finish(status=STATUS_FAILED)
    assert store.latest("backtest") is None, "失败的运行不应被当作最新结果"
    assert store.latest("backtest", status=STATUS_FAILED) is not None


def test_read_missing_returns_none(store):
    assert store.read("nope", "equity") is None
    assert store.read_text("nope", "report") is None
    assert store.load_manifest("nope") is None
    run = store.create_run("backtest", with_data_version=False).finish()
    assert store.read(run.run_id, "not_an_artifact") is None


def test_corrupted_manifest_is_skipped_not_fatal(store):
    d = store.root / "broken_run"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    good = store.create_run("backtest", with_data_version=False).finish()
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0].run_id == good.run_id


def test_prune_keeps_most_recent(store):
    for i in range(5):
        store.create_run("backtest", {"i": i}, with_data_version=False).finish()
    removed = store.prune(keep_last=2)
    assert removed == 3
    assert len(store.list_runs()) == 2
    assert store.latest("backtest").params["i"] == 4


def test_fingerprint_sensitive_to_params():
    a = fingerprint({"top_k": 20}, {}, "abc")
    b = fingerprint({"top_k": 30}, {}, "abc")
    c = fingerprint({"top_k": 20}, {}, "abc")
    assert a != b, "参数不同指纹必须不同"
    assert a == c, "同输入必须同指纹（可复现性校验的前提）"


def test_fingerprint_sensitive_to_data_and_code():
    base = fingerprint({"k": 1}, {"symbols": 100}, "abc")
    assert base != fingerprint({"k": 1}, {"symbols": 101}, "abc"), "数据版本必须参与"
    assert base != fingerprint({"k": 1}, {"symbols": 100}, "def"), "代码版本必须参与"


def test_fingerprint_ignores_key_order():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_path_of_resolves_inside_store(store):
    run = store.create_run("backtest", with_data_version=False)
    run.put_table("equity", pd.DataFrame({"v": [1]}))
    m = run.finish()
    p = store.path_of(m.run_id, "equity")
    assert p.exists()
    assert store.root in p.parents, "制品路径必须落在仓库内"
