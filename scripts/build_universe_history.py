"""从已积累的成分股快照构建 PIT 变更记录。

用法
----
```powershell
# 用 data/universe/{index}_YYYY-MM-DD.parquet 的历史快照反推变更
uv run python scripts/build_universe_history.py --index 000300

# 查看当前 PIT 覆盖情况
uv run python scripts/build_universe_history.py --index 000300 --describe-only
```

局限
----
快照差分的调样日期是**观测日**而非真实生效日，属下界近似。
要得到权威的 PIT 成分股，应导入中证指数调样公告或商业数据源，
用 `quart.data.universe_history.save_history()` 写入。
"""
from __future__ import annotations

import argparse

from quart.data.universe_history import (
    build_history_from_snapshots,
    describe,
    save_history,
    scan_existing_snapshots,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="构建成分股 PIT 历史")
    ap.add_argument("--index", default="000300", help="指数代码")
    ap.add_argument("--describe-only", action="store_true", help="只打印当前覆盖情况")
    args = ap.parse_args()

    if args.describe_only:
        print(describe(args.index))
        return

    snaps = scan_existing_snapshots(args.index)
    if len(snaps) < 2:
        raise SystemExit(
            f"快照不足（{len(snaps)} 个），至少需要 2 个不同日期的快照才能反推变更。\n"
            f"data/universe/{args.index}_YYYY-MM-DD.parquet 会随每次 update_data 累积，"
            f"请运行一段时间后重试。"
        )

    print(f"扫描到 {len(snaps)} 个快照: {min(snaps)} ~ {max(snaps)}")
    hist = build_history_from_snapshots(args.index, snaps)
    path = save_history(args.index, hist)

    n_in = int(hist["in_date"].notna().sum())
    n_out = int(hist["out_date"].notna().sum())
    print(f"写入 {path}")
    print(f"  记录数 {len(hist)}，其中 {n_out} 只已有调出日期")
    print(f"  覆盖 {hist['in_date'].min().date()} ~ {max(snaps)}")
    print(f"  当前状态: {describe(args.index)}")
    print()
    print("注意：快照差分只能还原「在两次采集之间发生了变化」，")
    print("      调样日期是观测日而非真实生效日，区间内先出再进的股票会被漏掉。")


if __name__ == "__main__":
    main()
