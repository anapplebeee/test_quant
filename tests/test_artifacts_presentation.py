"""制品展示层格式化测试。

这些函数原先写在 frontend/components 里（依赖 gradio，装不了就测不了）。
下沉到 api 层后可以断言：字段缺失/损坏/空目录时不崩，且降级可见。
"""
from __future__ import annotations

import pandas as pd
import pytest

from api import artifacts_api
from quart.data.artifacts import STATUS_FAILED, ArtifactStore


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_api, "_store", lambda: ArtifactStore(root=tmp_path / "af"))
    store = ArtifactStore(root=tmp_path / "af")

    r = store.create_run("backtest_lowvol_indz",
                         {"top_k": 20, "rebalance_days": 45}, with_data_version=False)
    r.put_table("equity", pd.DataFrame({"equity": [1.0, 1.1, 1.2]}))
    r.put_json("summary", {"cagr": 0.0701})
    r.add_metrics(cagr=0.0701, sharpe=0.67, n_trades=289,
                  param_stability={"top_k": 0.75})
    m_ok = r.finish()

    r2 = store.create_run("wfa_lowvol_indz", {"train": 504}, with_data_version=False)
    r2.add_metrics(decay=0.62, n_folds=6, n_folds_with_trades=5,
                   oos_cagr=0.031, oos_sharpe=0.4, oos_max_drawdown=-0.18,
                   param_stability={"top_k": 0.83})
    m_wfa = r2.finish()

    r3 = store.create_run("backtest_momentum", with_data_version=False)
    m_fail = r3.finish(status=STATUS_FAILED, error="数据已过期 9 天")

    return {"m_ok": m_ok, "m_wfa": m_wfa, "m_fail": m_fail}


# ---------------------------------------------------------------- runs_table


def test_runs_table_strips_metric_prefix(seeded):
    df = artifacts_api.runs_table()
    assert "cagr" in df.columns, "m_ 前缀应被去掉以便展示"
    assert not any(c.startswith("m_") for c in df.columns)


def test_runs_table_truncates_timestamp(seeded):
    df = artifacts_api.runs_table()
    assert all(len(str(t)) == 19 for t in df["created_at"]), "时间应截断到秒"


def test_runs_table_empty_when_no_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_api, "_store", lambda: ArtifactStore(root=tmp_path / "empty"))
    assert artifacts_api.runs_table().empty


# ---------------------------------------------------------------- run_detail_md


def test_detail_contains_full_provenance(seeded):
    md = artifacts_api.run_detail_md(seeded["m_ok"].run_id)
    # 四项可追溯要素必须都在：参数 / 数据 / 代码 / 指纹
    for token in (seeded["m_ok"].run_id, "指纹", "代码版本", "参数"):
        assert token in md
    assert "top_k" in md and "20" in md


def test_detail_lists_artifacts_with_row_counts(seeded):
    md = artifacts_api.run_detail_md(seeded["m_ok"].run_id)
    assert "equity" in md
    assert "3 行" in md, "应显示行数"


def test_detail_formats_floats_and_nested_dicts(seeded):
    md = artifacts_api.run_detail_md(seeded["m_ok"].run_id)
    assert "0.0701" in md
    assert '"top_k": 0.75' in md, "嵌套 dict 指标应 JSON 化而非 repr"


def test_detail_shows_error_for_failed_run(seeded):
    md = artifacts_api.run_detail_md(seeded["m_fail"].run_id)
    assert "failed" in md
    assert "数据已过期 9 天" in md


def test_detail_missing_run_is_a_message_not_exception(seeded):
    md = artifacts_api.run_detail_md("nonexistent")
    assert md == "*找不到该运行*"


def test_detail_rejects_traversal(seeded):
    assert artifacts_api.run_detail_md("../../etc/passwd") == "*找不到该运行*"


def test_detail_survives_missing_optional_fields(tmp_path, monkeypatch):
    """manifest 缺字段时不能 KeyError——旧格式/手工编辑都可能出现。"""
    store = ArtifactStore(root=tmp_path / "af")
    monkeypatch.setattr(artifacts_api, "_store", lambda: store)
    m = store.create_run("minimal", with_data_version=False)
    rid = m.finish().run_id

    path = store.root / rid / "manifest.json"
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("metrics", "artifacts", "data_version", "params"):
        raw.pop(key, None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    md = artifacts_api.run_detail_md(rid)
    assert rid in md, "缺可选字段仍应能渲染"


# ---------------------------------------------------------------- wfa_panel_md


def test_wfa_panel_shows_decay_verdict(seeded):
    md = artifacts_api.wfa_panel_md()
    assert "0.62" in md
    assert "过拟合" in md, "0.62 应判为存在过拟合"


@pytest.mark.parametrize("decay,expected", [
    (0.9, "稳健"),
    (0.6, "存在过拟合"),
    (0.2, "严重过拟合"),
])
def test_wfa_verdict_thresholds(tmp_path, monkeypatch, decay, expected):
    store = ArtifactStore(root=tmp_path / "af")
    monkeypatch.setattr(artifacts_api, "_store", lambda: store)
    r = store.create_run("wfa_x", with_data_version=False)
    r.add_metrics(decay=decay, n_folds=3, n_folds_with_trades=3)
    r.finish()

    assert expected in artifacts_api.wfa_panel_md()


def test_wfa_panel_warns_when_no_folds_traded(tmp_path, monkeypatch):
    """全空仓时衰减比无意义，必须明确告知而不是报个 0.00。"""
    store = ArtifactStore(root=tmp_path / "af")
    monkeypatch.setattr(artifacts_api, "_store", lambda: store)
    r = store.create_run("wfa_x", with_data_version=False)
    r.add_metrics(decay=0.0, n_folds=4, n_folds_with_trades=0)
    r.finish()

    md = artifacts_api.wfa_panel_md()
    assert "无成交" in md
    assert "无意义" in md


def test_wfa_panel_warns_on_partial_trading(tmp_path, monkeypatch):
    store = ArtifactStore(root=tmp_path / "af")
    monkeypatch.setattr(artifacts_api, "_store", lambda: store)
    r = store.create_run("wfa_x", with_data_version=False)
    r.add_metrics(decay=0.7, n_folds=6, n_folds_with_trades=4)
    r.finish()

    md = artifacts_api.wfa_panel_md()
    assert "2/6" in md, "应说明有 2 折未成交"


def test_wfa_panel_empty_state_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_api, "_store", lambda: ArtifactStore(root=tmp_path / "empty"))
    md = artifacts_api.wfa_panel_md()
    assert "暂无" in md
    assert "walk_forward.py" in md, "空状态应告诉用户怎么跑"


def test_wfa_panel_shows_oos_metrics_and_stability(seeded):
    md = artifacts_api.wfa_panel_md()
    assert "样本外合成净值" in md
    assert "参数一致率" in md
    assert "top_k" in md


# ---------------------------------------------------------------- 选项


def test_run_choices_format(seeded):
    ch = artifacts_api.run_choices()
    assert len(ch) == 3
    for c in ch:
        assert c.count("|") >= 2, f"选项格式应为 task | time | run_id: {c}"
    assert seeded["m_ok"].run_id in " ".join(ch)


def test_run_id_round_trip(seeded):
    ch = artifacts_api.run_choices()
    for c in ch:
        rid = artifacts_api.run_id_from_choice(c)
        assert rid and artifacts_api.get_run(rid) is not None


def test_run_id_from_empty(seeded):
    assert artifacts_api.run_id_from_choice("") == ""
    assert artifacts_api.run_id_from_choice(None) == ""


def test_run_choices_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_api, "_store", lambda: ArtifactStore(root=tmp_path / "empty"))
    assert artifacts_api.run_choices() == []
