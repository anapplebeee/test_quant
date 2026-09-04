"""抓取并入库真实 ETF 日线到平台数据仓库(data/daily, 裸6位码, 与A股共存)。

背景
----
ETF 动量轮动策略(quart/strategy/etf_momentum.py)以真实 ETF 为标的，平台本地无 ETF 行情。
本脚本用 akshare fund_etf_hist_em(东财源, 套用 quart/data/source_akshare.fetch_etf_daily 的
统一熔断/重试)抓取 9 只 ETF 前复权日线，经 BarStore.save 落盘到 data/daily/510300.parquet 等，
作为普通 symbol(裸6位码)与 A 股共存。

用法
----
    .venv/Scripts/python.exe scripts/update_etf.py              # 抓全部 ETF
    .venv/Scripts/python.exe scripts/update_etf.py --codes 510300,518880
    .venv/Scripts/python.exe scripts/update_etf.py --start 20200101
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.data.source_akshare import fetch_etf_daily  # noqa: E402
from quart.data.store import BarStore  # noqa: E402

#: ETF 动量轮动标的目录（对齐 quart/strategy/etf_momentum.py 默认 risk/defense）
ETF_CATALOG = [
    {"code": "510300", "name": "沪深300ETF"},
    {"code": "510500", "name": "中证500ETF"},
    {"code": "159915", "name": "创业板ETF"},
    {"code": "588000", "name": "科创50ETF"},
    {"code": "512890", "name": "红利低波ETF"},
    {"code": "518880", "name": "黄金ETF"},
    {"code": "159920", "name": "恒生ETF"},
    {"code": "513500", "name": "标普500ETF"},
    {"code": "511010", "name": "国债ETF(防御)"},
]


def update_etf(codes: list[str] | None = None, start: str = "20140101") -> dict:
    store = BarStore()
    targets = [item for item in ETF_CATALOG if codes is None or item["code"] in codes]
    if not targets:
        raise SystemExit(f"未匹配到 ETF 代码: {codes}")
    today = date.today().isoformat().replace("-", "")
    ok, empty, failed = [], [], []
    for item in targets:
        code = item["code"]
        try:
            df = fetch_etf_daily(code, start, today)
        except Exception as exc:  # noqa: BLE001
            failed.append((code, str(exc)))
            print(f"✗ {code} {item['name']}: {exc}", flush=True)
            continue
        if df is None or df.empty:
            empty.append(code)
            print(f"- {code} {item['name']}: 无数据(东财限流/未上市?)", flush=True)
            continue
        store.save(df, replace=True)
        ok.append(code)
        print(f"✓ {code} {item['name']}: {len(df)} 行 "
              f"{df['date'].min().date()}~{df['date'].max().date()}", flush=True)
    return {"ok": ok, "empty": empty, "failed": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default=None, help="逗号分隔 ETF 代码；缺省全部")
    parser.add_argument("--start", default="20140101")
    args = parser.parse_args()
    codes = args.codes.split(",") if args.codes else None
    result = update_etf(codes, start=args.start)
    print(f"RESULT: ok={len(result['ok'])} empty={result['empty']} failed={result['failed']}")
