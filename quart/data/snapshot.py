"""内容哈希数据快照（DATA-001，对应 TARGET_ARCHITECTURE_V3 §10）。

背景
----
此前数据版本只记录"股票数 + 首尾日期"，无法识别历史行情修订：
同一批文件哪怕某只票 2020 年的 close 被修正过，指纹也完全不变，
导致研究/回测不可复现、且无法审计数据变更。

设计
----
每个数据集（daily / index / factors / universe ...）构建一份
**不可变快照清单**（snapshot manifest）：

    snapshot_id
    dataset_name / schema_version
    partition -> size / row_count / min_date / max_date / content_hash
    universe_snapshot_id / security_master_version
    corporate_action_version / rule_book_version
    created_at / source / quality_status

- snapshot_id 由全部分区条目（相对路径 + 内容哈希 + 行数 + 字节数）
  的规范化 JSON 的 SHA-256 派生 —— **任何一行历史数据被修订都会
  改变 snapshot_id**（DATA-001 验收标准）；文件 mtime 变化不影响。
- ``diff_snapshots`` 输出新增/删除/修订的分区清单，用于数据修订审计。
- ``verify_snapshot`` 重新哈希落盘文件做完整性校验（漂移检测）。
- ``collect_pit_metadata`` 聚合 PIT 元数据（交易日历覆盖、股票池快照、
  上市日期覆盖、证券主数据版本），随快照一起落盘。

快照清单落盘在 ``data/meta/snapshots/<dataset>/<snapshot_id>.json``，
``latest.json`` 指向当前版本。快照本身不可变：重复构建同一内容得到
相同 snapshot_id，落盘是幂等覆盖（JSON 只含元数据，无历史风险）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from quart.config import data_root

SCHEMA_VERSION = 1
SNAPSHOT_ID_HEX = 16

MANIFEST_ROOT = Path(data_root()) / "meta" / "snapshots"

# 日期列候选（按优先级）：partition min/max 日期取第一个命中的列
_DATE_COLUMN_CANDIDATES = ["date", "trade_date", "first_date"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """流式计算文件内容的 SHA-256（十六进制）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class PartitionFingerprint:
    """单个分区文件的指纹。"""

    relpath: str
    size_bytes: int
    row_count: int
    min_date: str | None
    max_date: str | None
    content_hash: str


@dataclass
class SnapshotManifest:
    """数据集快照清单（TARGET_ARCHITECTURE_V3 §10 合同）。"""

    snapshot_id: str
    dataset_name: str
    schema_version: int
    created_at: str
    source: str
    quality_status: str
    partitions: list[PartitionFingerprint] = field(default_factory=list)
    file_count: int = 0
    total_rows: int = 0
    min_date: str | None = None
    max_date: str | None = None
    universe_snapshot_id: str | None = None
    security_master_version: str | None = None
    corporate_action_version: str | None = None
    rule_book_version: str | None = None
    pit_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SnapshotManifest:
        parts = [PartitionFingerprint(**p) for p in payload.pop("partitions", [])]
        return cls(partitions=parts, **payload)

    def partition_map(self) -> dict[str, PartitionFingerprint]:
        return {p.relpath: p for p in self.partitions}


# ---------------- 分区指纹 ----------------


def _parquet_min_max_date(path: Path) -> tuple[str | None, str | None]:
    """读取 parquet 的日期列首尾（缺失日期列时返回 (None, None)）。

    只读单列，避免整表加载；空表返回 (None, None)。
    """
    try:
        import pyarrow.parquet as pq

        schema = pq.read_schema(path)
        date_col = next(
            (c for c in _DATE_COLUMN_CANDIDATES if c in schema.names), None
        )
        if date_col is None:
            return None, None
        col = pq.read_table(path, columns=[date_col]).column(date_col).to_pandas()
        if col.empty:
            return None, None
        return str(pd.Timestamp(col.min()).date()), str(pd.Timestamp(col.max()).date())
    except Exception as exc:  # pragma: no cover - 非 parquet/损坏文件不应炸掉快照
        logger.warning("skip date range for {}: {}", path, exc)
        return None, None


def fingerprint_partition(path: Path, root: Path) -> PartitionFingerprint:
    """为单个数据文件生成指纹（内容哈希 + 行数 + 日期边界）。"""
    row_count = 0
    try:
        import pyarrow.parquet as pq

        row_count = pq.ParquetFile(path).metadata.num_rows
    except Exception:
        row_count = 0
    min_d, max_d = _parquet_min_max_date(path)
    return PartitionFingerprint(
        relpath=path.relative_to(root).as_posix(),
        size_bytes=path.stat().st_size,
        row_count=int(row_count),
        min_date=min_d,
        max_date=max_d,
        content_hash=sha256_file(path),
    )


def derive_snapshot_id(
    dataset_name: str, schema_version: int, partitions: list[PartitionFingerprint]
) -> str:
    """由分区条目的规范化 JSON 派生 snapshot_id。

    只包含内容相关字段（relpath/size/rows/hash），与 mtime 无关；
    历史数据任何修订都会改变该 ID（DATA-001 验收标准）。
    """
    payload = {
        "dataset_name": dataset_name,
        "schema_version": schema_version,
        "partitions": sorted(
            (
                [p.relpath, p.size_bytes, p.row_count, p.content_hash]
                for p in partitions
            ),
            key=lambda item: str(item[0]),
        ),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:SNAPSHOT_ID_HEX]


# ---------------- 快照构建 ----------------


def build_snapshot(
    dataset_name: str,
    root_dir: str | Path | None = None,
    *,
    schema_version: int = SCHEMA_VERSION,
    source: str = "local_parquet",
    quality_status: str = "unknown",
    universe_snapshot_id: str | None = None,
    security_master_version: str | None = None,
    corporate_action_version: str | None = None,
    rule_book_version: str | None = None,
    pit_metadata: dict[str, Any] | None = None,
) -> SnapshotManifest:
    """为 root_dir 下的全部数据文件构建快照清单。

    Parameters
    ----------
    dataset_name:
        数据集名（daily / index / factors / universe ...）。
    root_dir:
        数据集目录；默认按 dataset_name 猜测（daily -> data/daily），
        传 None 且目录不存在时抛 FileNotFoundError。
    """
    base = Path(data_root())
    if root_dir is None:
        root_dir = base / dataset_name
    root = Path(root_dir)
    if not root.exists():
        raise FileNotFoundError(f"dataset dir not found: {root}")

    files = sorted(p for p in root.rglob("*") if p.is_file())
    fingerprints = [fingerprint_partition(p, root) for p in files]
    date_bounds = [
        (p.min_date, p.max_date) for p in fingerprints if p.min_date and p.max_date
    ]
    meta_pit = dict(pit_metadata or {})
    if security_master_version is None:
        meta_pit.setdefault("security_master_version", None)

    manifest = SnapshotManifest(
        snapshot_id=derive_snapshot_id(dataset_name, schema_version, fingerprints),
        dataset_name=dataset_name,
        schema_version=schema_version,
        created_at=_now_iso(),
        source=source,
        quality_status=quality_status,
        partitions=fingerprints,
        file_count=len(fingerprints),
        total_rows=sum(p.row_count for p in fingerprints),
        min_date=min((d[0] for d in date_bounds), default=None),
        max_date=max((d[1] for d in date_bounds), default=None),
        universe_snapshot_id=universe_snapshot_id,
        security_master_version=security_master_version,
        corporate_action_version=corporate_action_version,
        rule_book_version=rule_book_version,
        pit_metadata=meta_pit,
    )
    logger.info(
        "snapshot built: dataset={} id={} files={} rows={}",
        dataset_name, manifest.snapshot_id, manifest.file_count, manifest.total_rows,
    )
    return manifest


# ---------------- 落盘与读取 ----------------


def snapshot_dir(dataset_name: str, base: Path | None = None) -> Path:
    return (base or MANIFEST_ROOT) / dataset_name


def save_manifest(manifest: SnapshotManifest, base: Path | None = None) -> Path:
    """保存快照清单并更新 latest 指针（幂等）。"""
    d = snapshot_dir(manifest.dataset_name, base)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{manifest.snapshot_id}.json"
    path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest = d / "latest.json"
    latest.write_text(
        json.dumps({"snapshot_id": manifest.snapshot_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("manifest saved: {}", path)
    return path


def list_snapshots(dataset_name: str, base: Path | None = None) -> list[str]:
    """列出某数据集已有的全部 snapshot_id（不含 latest 指针）。"""
    d = snapshot_dir(dataset_name, base)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json") if p.name != "latest.json")


def load_manifest(
    dataset_name: str, snapshot_id: str | None = None, base: Path | None = None
) -> SnapshotManifest | None:
    """读取快照清单；snapshot_id 为 None 时读 latest，缺失返回 None。"""
    d = snapshot_dir(dataset_name, base)
    if snapshot_id is None:
        latest = d / "latest.json"
        if not latest.exists():
            return None
        snapshot_id = json.loads(latest.read_text(encoding="utf-8"))["snapshot_id"]
    path = d / f"{snapshot_id}.json"
    if not path.exists():
        return None
    return SnapshotManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------- 修订识别与校验 ----------------


def diff_snapshots(
    old: SnapshotManifest, new: SnapshotManifest
) -> dict[str, list[str]]:
    """对比两份快照，识别数据修订。

    Returns
    -------
    dict(revised=[...], added=[...], removed=[...], unchanged=[...])
    revised 即"历史修订"的分区列表（同路径但内容哈希不同）。
    """
    old_map, new_map = old.partition_map(), new.partition_map()
    revised = sorted(
        rp for rp, fp in new_map.items()
        if rp in old_map and old_map[rp].content_hash != fp.content_hash
    )
    added = sorted(rp for rp in new_map if rp not in old_map)
    removed = sorted(rp for rp in old_map if rp not in new_map)
    unchanged = sorted(rp for rp in new_map if rp in old_map and rp not in revised)
    return {"revised": revised, "added": added, "removed": removed, "unchanged": unchanged}


def verify_snapshot(
    manifest: SnapshotManifest, root_dir: str | Path | None = None
) -> list[str]:
    """完整性校验：重新哈希落盘文件并比对清单。

    Returns
    -------
    异常清单（空列表 = 通过）。异常包括：文件缺失、内容哈希不匹配。
    """
    base = Path(root_dir) if root_dir else Path(data_root()) / manifest.dataset_name
    problems: list[str] = []
    for p in manifest.partitions:
        path = base / p.relpath
        if not path.exists():
            problems.append(f"missing: {p.relpath}")
        elif sha256_file(path) != p.content_hash:
            problems.append(f"hash mismatch: {p.relpath}")
    return problems


# ---------------- PIT 元数据 ----------------


def collect_pit_metadata(base: Path | None = None) -> dict[str, Any]:
    """聚合 PIT 元数据（覆盖度而非内容本身，供快照与研究指纹引用）。

    覆盖四类来源，任一缺失以 None 记录而不是失败：
    - 交易日历（data/meta/trading_calendar.csv）；
    - 股票池快照（data/universe/<code>_<date>.parquet）；
    - 上市日期覆盖（data/universe/list_dates.parquet）；
    - 证券主数据版本（data/meta/security_master.parquet，如有）。
    """
    root = Path(base) if base else Path(data_root())
    meta: dict[str, Any] = {}

    cal_path = root / "meta" / "trading_calendar.csv"
    if cal_path.exists():
        try:
            cal = pd.read_csv(cal_path)
            col = cal.columns[0]
            dates = pd.to_datetime(cal[col])
            meta["trading_calendar"] = {
                "sessions": len(cal),
                "min_date": str(dates.min().date()),
                "max_date": str(dates.max().date()),
            }
        except Exception as exc:
            logger.warning("pit: calendar unreadable: {}", exc)

    universe_dir = root / "universe"
    if universe_dir.exists():
        import re

        pattern = re.compile(r"^(?P<code>[A-Za-z0-9]+)_(?P<date>\d{4}-\d{2}-\d{2})\.parquet$")
        snapshots: list[dict[str, str]] = []
        for p in universe_dir.glob("*_*.parquet"):
            m = pattern.match(p.name)
            if m:
                snapshots.append({"code": m.group("code"), "date": m.group("date")})
        meta["universe_snapshots"] = {
            "count": len(snapshots),
            "dates": sorted({s["date"] for s in snapshots}),
        }

    list_dates = universe_dir / "list_dates.parquet" if universe_dir.exists() else None
    if list_dates is not None and list_dates.exists():
        try:
            ld = pd.read_parquet(list_dates)
            first = pd.to_datetime(ld["first_date"])
            meta["list_dates"] = {"symbols": len(ld), "min": str(first.min().date())}
        except Exception as exc:
            logger.warning("pit: list_dates unreadable: {}", exc)

    sm_path = root / "meta" / "security_master.parquet"
    if sm_path.exists():
        try:
            from quart.data.security_master import load_master_version

            meta["security_master_version"] = load_master_version(sm_path)
        except Exception as exc:  # pragma: no cover
            logger.warning("pit: security_master unreadable: {}", exc)

    return meta
