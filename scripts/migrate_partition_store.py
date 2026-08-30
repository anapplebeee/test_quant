"""把行情仓库从"单股全史单文件"迁移到按年分区布局。

为什么要迁移
------------
1. **增量写入**：旧布局每只票增量 1 天也要重写它的**全史**文件。
   5000 只票每天做一次 read-modify-write，IO 是 O(总数据量)。
   分区后只重写当年分区，IO 降到 O(当年数据量)。
2. **查询裁剪**：回测通常只取最近 1-2 年，分区后 DuckDB 用
   `hive_partitioning` 直接跳过无关年份目录，全市场扫描省掉大部分 IO。
3. **消除超长 SQL**：旧 `load()` 把 5000+ 文件路径拼进一条 SQL 字符串。

用法
----
```powershell
# 预演（不写盘，只看会迁移多少）
uv run python scripts/migrate_partition_store.py --dry-run

# 执行迁移（旧文件迁移成功后删除）
uv run python scripts/migrate_partition_store.py
```

安全性
------
* 幂等：已存在的年份分区会做 merge + 去重，重复运行不会丢数据
* 先写 `.tmp` 再 `os.replace` 原子替换，中断不会留下半截文件
* 只有确认整只票的所有年份都写成功，才删除旧文件
"""
from __future__ import annotations

import argparse

from quart.data.store import BarStore
from quart.data.universe import data_root


def main() -> None:
    ap = argparse.ArgumentParser(description="迁移行情仓库到按年分区布局")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--root", default=None, help="数据仓库根目录（默认取配置）")
    args = ap.parse_args()

    store = BarStore(root=args.root or data_root())
    root = store.root

    flat_daily = sorted(store.daily_dir.glob("*.parquet"))
    flat_index = sorted(store.index_dir.glob("*.parquet"))
    total_files = len(flat_daily) + len(flat_index)

    print(f"数据仓库: {root}")
    print(f"当前布局: {'分区' if store.partitioned else '单股单文件'}")
    print(f"待迁移文件: {total_files}（daily {len(flat_daily)} / index {len(flat_index)}）")

    if total_files == 0:
        print("没有需要迁移的文件（已是分区布局或仓库为空）。")
        return

    years = store._partition_years(store.daily_dir)
    print(f"已有分区目录: {years or '无'}")

    if args.dry_run:
        print("\n[dry-run] 未做任何改动。去掉 --dry-run 执行迁移。")
        return

    print("\n开始迁移...")
    stats = store.migrate_to_partitioned(
        progress=lambda s: print(f"  {s}") if total_files <= 50 else None
    )
    print(
        f"\n完成：迁移 {stats['symbols']} 只标的，写出 {stats['files']} 个分区文件，"
        f"跳过 {stats['skipped']} 个。"
    )
    print(f"迁移后布局: {'分区' if store.partitioned else '单股单文件'}")

    # 迁移后自检：标的数量不应减少
    n_syms = len(store.symbols())
    print(f"仓库现有标的数量: {n_syms}")
    if n_syms == 0:
        raise SystemExit("迁移后标的数量为 0，请检查数据目录！")


if __name__ == "__main__":
    main()
