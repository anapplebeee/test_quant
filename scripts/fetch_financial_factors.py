"""抓取沪深300成分股财务因子（季频）到 data/factors/financials.parquet。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data.factors import build_factor_store
from quart.data.universe import get_constituents


def main() -> None:
    symbols = get_constituents("000300")
    print(f"universe: {len(symbols)}", flush=True)
    res = build_factor_store(symbols)
    print(f"RESULT: {res}", flush=True)


if __name__ == "__main__":
    main()
