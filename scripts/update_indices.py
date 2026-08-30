"""批量更新常用指数日线（上证/深证/创业板/中证/科创等）。

用法:
    .venv/Scripts/python.exe scripts/update_indices.py          # 全量
    .venv/Scripts/python.exe scripts/update_indices.py --codes 000001,399006
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data.index_catalog import INDEX_CATALOG
from quart.data.source_akshare import fetch_index_daily
from quart.data.store import BarStore


def update_indices(codes: list[str] | None = None, start: str = "20190101") -> dict:
    store = BarStore()
    targets = [item for item in INDEX_CATALOG if codes is None or item["code"] in codes]
    if not targets:
        raise SystemExit(f"未匹配到指数代码: {codes}")
    today = date.today().isoformat().replace("-", "")
    ok, empty, failed = [], [], []
    for item in targets:
        code = item["code"]
        try:
            df = fetch_index_daily(code, start, today)
        except Exception as exc:
            failed.append((code, str(exc)))
            print(f"✗ {code} {item['name']}: {exc}", flush=True)
            continue
        if df is None or df.empty:
            empty.append(code)
            print(f"- {code} {item['name']}: 无数据", flush=True)
            continue
        store.save(df, replace=True)
        ok.append(code)
        print(f"✓ {code} {item['name']}: {len(df)} 行", flush=True)
    return {"ok": ok, "empty": empty, "failed": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default=None, help="逗号分隔指数代码；缺省更新全部")
    parser.add_argument("--start", default="20190101")
    args = parser.parse_args()
    codes = args.codes.split(",") if args.codes else None
    result = update_indices(codes, start=args.start)
    print(f"RESULT: ok={len(result['ok'])} empty={result['empty']} failed={result['failed']}")
