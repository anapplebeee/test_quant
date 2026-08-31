"""DATA-001 回归测试：内容哈希快照、修订识别、PIT 元数据、证券主数据。

核心验收标准（DEVELOPMENT_COORDINATION.md 泳道 B）：
**历史修订会改变 snapshot_id** —— 任一分区文件的历史内容被修改，
重建快照必须得到不同的 snapshot_id；纯 mtime 变化不影响。
"""
from __future__ import annotations

import json
import shutil

import pandas as pd
import pytest

from quart.data import snapshot as snap
from quart.data.security_master import SecurityMaster, source_mapping_summary


def _write_daily_bars(root, symbol: str, date: str, close: float) -> None:
    year = date[:4]
    d = root / "daily" / f"year={year}"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "date": [pd.Timestamp(date)],
        "symbol": [symbol],
        "open": [close], "high": [close], "low": [close], "close": [close],
        "volume": [1000.0], "amount": [close * 1000.0],
    }).to_parquet(d / f"{symbol}_{year}.parquet", index=False)


@pytest.fixture
def data_root(tmp_path):
    _write_daily_bars(tmp_path, "600519", "2024-01-02", 100.0)
    _write_daily_bars(tmp_path, "600519", "2024-01-03", 101.0)
    _write_daily_bars(tmp_path, "000001", "2024-01-02", 10.0)
    return tmp_path


# ---------------- 快照 ----------------

def test_snapshot_deterministic_for_identical_content(data_root, tmp_path):
    """内容相同的两份数据 → 相同 snapshot_id（与 mtime 无关）。"""
    s1 = snap.build_snapshot("daily", data_root / "daily")
    copy = tmp_path.parent / "copy_root"
    shutil.copytree(data_root / "daily", copy / "daily", dirs_exist_ok=True)
    # 让文件 mtime 变化，哈希不应受影响
    for p in (copy / "daily").rglob("*.parquet"):
        p.touch()
    s2 = snap.build_snapshot("daily", copy / "daily")
    assert s1.snapshot_id == s2.snapshot_id


def test_history_revision_changes_snapshot_id(data_root):
    """验收标准：修改历史行情（2024-01-02 的 close）必须改变 snapshot_id。"""
    s_old = snap.build_snapshot("daily", data_root / "daily")
    _write_daily_bars(data_root, "600519", "2024-01-02", 999.0)  # 修订历史
    s_new = snap.build_snapshot("daily", data_root / "daily")
    assert s_old.snapshot_id != s_new.snapshot_id


def test_diff_identifies_revised_partition(data_root):
    s_old = snap.build_snapshot("daily", data_root / "daily")
    _write_daily_bars(data_root, "600519", "2024-01-02", 888.0)
    _write_daily_bars(data_root, "600036", "2024-01-02", 30.0)  # 新增
    s_new = snap.build_snapshot("daily", data_root / "daily")
    d = snap.diff_snapshots(s_old, s_new)
    assert d["revised"] == ["year=2024/600519_2024.parquet"]  # 同路径，内容哈希不同
    assert d["added"] == ["year=2024/600036_2024.parquet"]
    assert d["removed"] == []
    assert "year=2024/000001_2024.parquet" in d["unchanged"]


def test_verify_detects_tampering(data_root):
    manifest = snap.build_snapshot("daily", data_root / "daily")
    assert snap.verify_snapshot(manifest, data_root / "daily") == []
    _write_daily_bars(data_root, "000001", "2024-01-02", 11.0)
    problems = snap.verify_snapshot(manifest, data_root / "daily")
    assert problems == ["hash mismatch: year=2024/000001_2024.parquet"]


def test_manifest_roundtrip_and_latest(data_root, tmp_path):
    manifest = snap.build_snapshot("daily", data_root / "daily")
    base = tmp_path / "meta" / "snapshots"
    snap.save_manifest(manifest, base)
    loaded = snap.load_manifest("daily", manifest.snapshot_id, base)
    assert loaded is not None
    assert loaded.snapshot_id == manifest.snapshot_id
    assert loaded.total_rows == manifest.total_rows
    latest = snap.load_manifest("daily", None, base)
    assert latest is not None and latest.snapshot_id == manifest.snapshot_id
    assert snap.list_snapshots("daily", base) == [manifest.snapshot_id]
    assert json.loads((base / "daily" / "latest.json").read_text(encoding="utf-8"))


def test_manifest_contract_fields(data_root):
    """§10 快照清单合同字段必须齐备。"""
    m = snap.build_snapshot("daily", data_root, security_master_version="abc123")
    for attr in (
        "snapshot_id", "dataset_name", "schema_version", "created_at", "source",
        "quality_status", "partitions", "universe_snapshot_id",
        "security_master_version", "corporate_action_version", "rule_book_version",
        "pit_metadata",
    ):
        assert hasattr(m, attr)
    p = m.partitions[0]
    for attr in ("relpath", "size_bytes", "row_count", "min_date", "max_date", "content_hash"):
        assert getattr(p, attr) is not None
    assert m.min_date == "2024-01-02" and m.max_date == "2024-01-03"


def test_build_snapshot_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        snap.build_snapshot("daily", tmp_path / "nope")


# ---------------- PIT 元数据 ----------------

def test_collect_pit_metadata(tmp_path):
    (tmp_path / "meta").mkdir()
    pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5)}).to_csv(
        tmp_path / "meta" / "trading_calendar.csv", index=False
    )
    (tmp_path / "universe").mkdir()
    pd.DataFrame({"symbol": ["600519"], "first_date": [pd.Timestamp("2024-01-01")]}).to_parquet(
        tmp_path / "universe" / "list_dates.parquet", index=False
    )
    meta = snap.collect_pit_metadata(tmp_path)
    assert meta["trading_calendar"]["sessions"] == 5
    assert meta["list_dates"]["symbols"] == 1
    assert "security_master_version" not in meta  # 无主数据时不出现


def test_pit_metadata_attached_to_manifest(data_root):
    manifest = snap.build_snapshot("daily", data_root, pit_metadata=snap.collect_pit_metadata(data_root))
    assert "trading_calendar" not in manifest.pit_metadata  # 合成目录无日历，不炸
    assert isinstance(manifest.pit_metadata, dict)


# ---------------- 证券主数据 ----------------

@pytest.fixture
def sm_root(tmp_path):
    pd.DataFrame({"code": ["600519", "000001", "301999"]}).to_parquet(
        tmp_path / "stock_names.parquet", index=False
    )
    (tmp_path / "universe").mkdir()
    pd.DataFrame({
        "symbol": ["600519", "000001"],
        "first_date": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-02")],
    }).to_parquet(tmp_path / "universe" / "list_dates.parquet", index=False)
    return tmp_path


def test_master_from_local_schema(sm_root):
    master = SecurityMaster.from_local(sm_root)
    cols = ["symbol", "exchange", "board", "security_type", "listed_at", "delisted_at",
            "status", "status_effective_from", "status_effective_to",
            "lot_size", "tick_size", "price_limit_rule", "settlement_rule"]
    assert list(master.table.columns) == cols
    assert master.validate() == []
    row = master.table.set_index("symbol").loc["301999"]
    assert row["exchange"] == "SZSE" and row["board"] == "CHINEXT"
    assert row["price_limit_rule"] == 0.20 and row["settlement_rule"] == "T+1"


def test_master_as_of_pit_query(sm_root):
    master = SecurityMaster.from_local(sm_root)
    before = master.as_of("2024-01-01")
    assert before.empty  # 均未上市
    after = master.as_of("2024-06-01")
    assert set(after["symbol"]) == {"600519", "000001"}  # 301999 无上市日记录


def test_master_status_effective_interval(tmp_path):
    df = pd.DataFrame([{
        "symbol": "600519", "listed_at": pd.Timestamp("2024-01-02"),
        "status": "st", "status_effective_from": pd.Timestamp("2024-03-01"),
        "status_effective_to": pd.Timestamp("2024-04-01"),
    }])
    master = SecurityMaster(df)
    assert master.status_as_of("600519", "2024-03-15")["status"] == "st"
    assert master.status_as_of("600519", "2024-02-01") is None
    assert master.status_as_of("600519", "2024-04-01") is None  # 区间含头不含尾
    assert master.status_as_of("999999", "2024-03-15") is None


def test_master_version_changes_on_field_change(sm_root):
    master = SecurityMaster.from_local(sm_root)
    v1 = master.version()
    master.table.loc[0, "delisted_at"] = pd.Timestamp("2025-01-01")
    assert master.version() != v1  # 主数据内容变化 → 版本变化


def test_master_save_load_roundtrip(sm_root, tmp_path):
    master = SecurityMaster.from_local(sm_root)
    path = tmp_path / "security_master.parquet"
    master.save(path)
    loaded = SecurityMaster.load(path)
    assert loaded.version() == master.version()
    assert loaded.validate() == []


def test_master_validate_detects_inverted_interval(tmp_path):
    df = pd.DataFrame([{
        "symbol": "600519", "status": "st",
        "status_effective_from": pd.Timestamp("2024-04-01"),
        "status_effective_to": pd.Timestamp("2024-03-01"),
    }])
    problems = SecurityMaster(df).validate()
    assert any("inverted" in p for p in problems)


def test_source_mapping_covers_pending_fields():
    """来源映射必须显式声明 pending 字段（DATA-001 交付物）。"""
    summary = source_mapping_summary()
    assert {"listed_at", "delisted_at", "status(ST/风险警示/停复牌)"} <= set(summary["fields"])
    assert (summary["status"] == "pending").any()
    assert (summary["status"] == "available").any()
