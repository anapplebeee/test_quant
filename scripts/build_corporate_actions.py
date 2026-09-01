"""导入、校验和版本化 A 股公司行为 PIT 账本（DATA-002）。

上游数据商字段各异，先以 CSV/Parquet 的受控导入建立唯一事实源；脚本不会把
行情复权因子反推成公司行为，也不会在无公告/可得日期时猜测历史可得性。

示例：
    uv run python scripts/build_corporate_actions.py --input data/raw/actions.csv --source cninfo
    uv run python scripts/build_corporate_actions.py --input actions.parquet --source wind --replace
    uv run python scripts/build_corporate_actions.py --input actions.csv --validate-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data.corporate_actions import CorporateActionLedger, normalize_corporate_actions

console = Console()


def _read_input(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ValueError("input must be .csv, .parquet or .pq")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="公司行为原始 CSV/Parquet")
    parser.add_argument("--source", default=None, help="来源标识；不传时取文件名")
    parser.add_argument("--replace", action="store_true", help="覆盖现有账本，而非按 action_id 合并")
    parser.add_argument("--validate-only", action="store_true", help="只校验输入，不写入")
    args = parser.parse_args()

    raw = _read_input(args.input)
    incoming = normalize_corporate_actions(raw, source=args.source or args.input.stem)
    candidate = CorporateActionLedger(incoming)
    if args.validate_only:
        console.print(f"[green]valid[/] actions={len(candidate.table)} version={candidate.version()}")
        return

    if args.replace:
        merged = candidate.table
    else:
        try:
            existing = CorporateActionLedger.load().table
        except FileNotFoundError:
            existing = pd.DataFrame(columns=incoming.columns)
        # 同一自然键可拥有多份修订；相同内容 revision_id 去重，不能覆盖旧版本。
        merged = pd.concat([existing, incoming], ignore_index=True).drop_duplicates("revision_id", keep="last")
    ledger = CorporateActionLedger(merged)
    target = ledger.save()
    console.print(f"[green]saved[/] actions={len(ledger.table)} version={ledger.version()} -> {target}")


if __name__ == "__main__":
    main()
