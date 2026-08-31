"""验证 update_data.py 数据更新后自动构建快照（DATA-001 集成）。

协调文档 10.1 Research Release 要求"固定数据快照"——快照必须是数据更新的
自动产出。此测试验证：update_data 调用 update_universe_data 后，会调用
build_snapshot 并保存 manifest，把 snapshot_id 记录进更新状态。
"""
from __future__ import annotations

import json

import pytest


def test_update_data_builds_snapshot_after_update(monkeypatch, tmp_path):
    """数据更新成功后应自动构建快照并记录 snapshot_id。"""
    import scripts.update_data as ud

    # 打桩数据更新：返回统计，不真跑网络
    called_update = {"n": 0}

    def fake_update_universe_data(index, codes, **kw):
        called_update["n"] += 1
        return {"total": len(codes), "ok": len(codes), "empty": 0, "failed": 0,
                "refreshed": 0, "empty_symbols": [], "failed_symbols": []}

    # 打桩快照：验证被调用并返回可记录的 snapshot_id
    built = {"calls": [], "saved": []}

    def fake_build_snapshot(dataset_name, quality_status="", pit_metadata=None, **kw):
        built["calls"].append(dataset_name)
        m = type("M", (), {
            "snapshot_id": f"snap_{dataset_name}", "file_count": 1, "total_rows": 1,
            "dataset_name": dataset_name,
        })()
        return m

    def fake_save_manifest(manifest):
        built["saved"].append(manifest.dataset_name)

    monkeypatch.setattr(ud, "update_universe_data", fake_update_universe_data)
    monkeypatch.setattr(ud, "filter_mainboard", lambda codes: codes)
    monkeypatch.setattr(ud, "filter_st", lambda codes: codes)
    monkeypatch.setattr(ud, "get_constituents", lambda index: ["600519"])
    monkeypatch.setattr(ud, "data_root", lambda: tmp_path)

    # patch quart.data.snapshot 模块（update_data 内部 import）
    import quart.data.snapshot as snap
    monkeypatch.setattr(snap, "build_snapshot", fake_build_snapshot)
    monkeypatch.setattr(snap, "save_manifest", fake_save_manifest)
    monkeypatch.setattr(snap, "collect_pit_metadata", lambda: {})

    # 运行 main（universe=index 会走 get_constituents）
    from unittest.mock import patch
    import sys
    with patch.object(sys, "argv", ["update_data.py", "--universe", "index",
                                    "--index", "000300", "--start", "2024-01-01"]):
        # update_data.main 里 load_config 需要真实 config；直接调用会因真实
        # universe 模式跑真实 update。为隔离，这里通过 monkeypatch 已替换
        # update_universe_data，因此 update 不真跑。但 get_constituents 也被
        # patch 成返回固定列表。
        ud.main()

    # 更新被调用
    assert called_update["n"] == 1
    # 快照应被构建（daily/index 默认数据集）
    assert "daily" in built["calls"]
    assert "index" in built["calls"]
    # manifest 应被保存
    assert built["saved"] == ["daily", "index"]

    # 快照 ID 应记录进更新状态
    status_path = tmp_path / "meta" / "last_data_update.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["snapshot_ids"] == {"daily": "snap_daily", "index": "snap_index"}


def test_snapshot_build_failure_does_not_block_update(monkeypatch, tmp_path):
    """快照构建失败不应阻断数据更新（数据已落盘，只告警）。"""
    import scripts.update_data as ud
    import logging

    def fake_update_universe_data(index, codes, **kw):
        return {"total": 1, "ok": 1, "empty": 0, "failed": 0, "refreshed": 0,
                "empty_symbols": [], "failed_symbols": []}

    monkeypatch.setattr(ud, "update_universe_data", fake_update_universe_data)
    monkeypatch.setattr(ud, "filter_st", lambda codes: codes)
    monkeypatch.setattr(ud, "get_constituents", lambda index: ["600519"])
    monkeypatch.setattr(ud, "data_root", lambda: tmp_path)

    import quart.data.snapshot as snap

    def failing_build_snapshot(dataset_name, **kw):
        raise FileNotFoundError(f"dataset dir not found: {dataset_name}")

    monkeypatch.setattr(snap, "build_snapshot", failing_build_snapshot)
    monkeypatch.setattr(snap, "save_manifest", lambda m: None)
    monkeypatch.setattr(snap, "collect_pit_metadata", lambda: {})

    from unittest.mock import patch
    import sys
    with patch.object(sys, "argv", ["update_data.py", "--universe", "index"]):
        # 不抛异常即通过（快照失败被捕获并告警）
        ud.main()

    # 更新状态仍应写入（无 snapshot_ids）
    status_path = tmp_path / "meta" / "last_data_update.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert "snapshot_ids" not in status or status["snapshot_ids"] == {}
