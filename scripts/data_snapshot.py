"""数据快照构建 / 校验 / 修订对比 CLI（DATA-001）。

为本地数据集构建内容哈希快照清单（TARGET_ARCHITECTURE_V3 §10），
并对拍修订、校验完整性、装配证券主数据。

用法：
    uv run python scripts/data_snapshot.py build --dataset daily --dataset index
    uv run python scripts/data_snapshot.py build --dataset daily --verify
    uv run python scripts/data_snapshot.py verify --dataset daily
    uv run python scripts/data_snapshot.py diff --dataset daily --from <snap_id> [--to <snap_id>]
    uv run python scripts/data_snapshot.py master --build
    uv run python scripts/data_snapshot.py master --as-of 2024-06-01
    uv run python scripts/data_snapshot.py master --sources
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data import snapshot as snap
from quart.data.security_master import (
    SecurityMaster,
    source_mapping_summary,
)

console = Console()


def _datasets_from_args(args: argparse.Namespace) -> list[str]:
    return args.dataset or ["daily", "index"]


def _attach_pit(manifest: snap.SnapshotManifest) -> snap.SnapshotManifest:
    manifest.pit_metadata = snap.collect_pit_metadata()
    manifest.security_master_version = manifest.pit_metadata.get("security_master_version")
    manifest.corporate_action_version = manifest.pit_metadata.get("corporate_action_version")
    if manifest.security_master_version is None:
        console.print("[yellow]security_master.parquet 不存在，先运行 master --build[/]")
    if manifest.corporate_action_version is None:
        console.print("[yellow]corporate_actions.parquet 不存在，尚未绑定公司行为账本[/]")
    return manifest


def cmd_build(args: argparse.Namespace) -> None:
    for ds in _datasets_from_args(args):
        old = snap.load_manifest(ds)
        manifest = snap.build_snapshot(ds, quality_status="scanned")
        _attach_pit(manifest)
        if old is not None:
            d = snap.diff_snapshots(old, manifest)
            if d["revised"] or d["added"] or d["removed"]:
                console.print(
                    f"[bold yellow]revision detected vs {old.snapshot_id}: "
                    f"revised={len(d['revised'])} added={len(d['added'])} "
                    f"removed={len(d['removed'])}[/]"
                )
            else:
                console.print(f"[green]no content change vs {old.snapshot_id}[/]")
        snap.save_manifest(manifest)
        console.print(
            f"[bold]{ds}[/] snapshot_id={manifest.snapshot_id} files={manifest.file_count} rows={manifest.total_rows}"
        )
        if args.verify:
            problems = snap.verify_snapshot(manifest)
            if problems:
                console.print(f"[red]verify failed: {problems[:10]}[/]")
            else:
                console.print("[green]verify ok (content hashes match)[/]")


def cmd_verify(args: argparse.Namespace) -> None:
    for ds in _datasets_from_args(args):
        manifest = snap.load_manifest(ds, args.snapshot_id)
        if manifest is None:
            console.print(f"[red]no manifest for {ds}[/]")
            continue
        problems = snap.verify_snapshot(manifest)
        if problems:
            console.print(f"[red]{ds} {manifest.snapshot_id}: {len(problems)} problem(s)[/]")
            for p in problems[:20]:
                console.print(f"  - {p}")
        else:
            console.print(f"[green]{ds} {manifest.snapshot_id}: verify ok[/]")


def cmd_diff(args: argparse.Namespace) -> None:
    old = snap.load_manifest(args.dataset, args.snapshot_id_from)
    new = snap.load_manifest(args.dataset, args.snapshot_id_to)
    if old is None or new is None:
        console.print("[red]manifest not found[/]")
        return
    d = snap.diff_snapshots(old, new)
    table = Table(title=f"{args.dataset}: {old.snapshot_id} -> {new.snapshot_id}")
    table.add_column("kind")
    table.add_column("partitions", overflow="fold")
    for kind in ("revised", "added", "removed", "unchanged"):
        table.add_row(kind, f"{len(d[kind])}: " + ", ".join(d[kind][:20]))
    console.print(table)
    if d["revised"]:
        console.print("[bold yellow]历史修订已识别（内容哈希变化）[/]")


def cmd_master(args: argparse.Namespace) -> None:
    if args.build:
        master = SecurityMaster.from_local()
        problems = master.validate()
        if problems:
            console.print(f"[red]validate problems: {problems}[/]")
        path = master.save()
        console.print(f"security master saved -> {path}")
        console.print(f"security_master_version = [bold]{master.version()}[/]")
        return
    if args.version:
        master = SecurityMaster.load()
        console.print(master.version())
        return
    if args.as_of:
        master = SecurityMaster.load()
        df = master.as_of(args.as_of)
        console.print(f"as_of {args.as_of}: {len(df)} securities")
        console.print(df.head(10).to_string())
        return
    if args.sources:
        console.print(source_mapping_summary().to_string(index=False))
        return
    console.print("[yellow]choose one of --build/--version/--as-of/--sources[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="构建快照清单（可多数据集）")
    p_build.add_argument("--dataset", action="append", help="数据集名（默认 daily/index）")
    p_build.add_argument("--verify", action="store_true", help="构建后立即校验哈希")
    p_build.set_defaults(fn=cmd_build)

    p_verify = sub.add_parser("verify", help="校验快照完整性（重哈希）")
    p_verify.add_argument("--dataset", action="append")
    p_verify.add_argument("--snapshot-id", default=None)
    p_verify.set_defaults(fn=cmd_verify)

    p_diff = sub.add_parser("diff", help="对比两份快照识别修订")
    p_diff.add_argument("--dataset", required=True)
    p_diff.add_argument("--from", dest="snapshot_id_from", default=None)
    p_diff.add_argument("--to", dest="snapshot_id_to", default=None)
    p_diff.set_defaults(fn=cmd_diff)

    p_master = sub.add_parser("master", help="证券主数据：构建/版本/PIT 查询/来源映射")
    p_master.add_argument("--build", action="store_true")
    p_master.add_argument("--version", action="store_true")
    p_master.add_argument("--as-of", default=None)
    p_master.add_argument("--sources", action="store_true")
    p_master.set_defaults(fn=cmd_master)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
