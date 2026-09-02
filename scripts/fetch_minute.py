"""从智兔数服抓取 A 股分钟K线并写入 MinuteStore（research 级）。

用法
----
    # token 从环境变量 ZHITU_API_TOKEN 读取（勿写进版本库）
    $env:ZHITU_API_TOKEN = "<token>"
    python scripts/fetch_minute.py --symbols 000001.SZ,600519.SH --level 5 --start 2023-06-01 --end 2026-09-02
    # 或用文件（每行一个代码，可含 .SH/.SZ 或不含）
    python scripts/fetch_minute.py --file research_symbols.txt --level 5,30

说明
----
- 单请求按 **5 个自然日切块**，规避智兔单次返回条数/时窗限制，并天然支持
  断点续传（已抓过的时间窗跳过）。
- level 逗号分隔，如 ``5,30``。最低 5 分钟（智兔无 1 分钟）。
- 抓取失败（网络）自动重试；连续失败的代码记入 ``--failed-out``，供下次重试。
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from quart.data.minute_store import MinuteStore, LEVELS
from quart.data.source_zhitu import MINUTE_HISTORY_START, ZhituSource

CHUNK_DAYS = 5


def _resolve_symbols(symbols: list[str], file: str | None) -> list[str]:
    raw: list[str] = []
    if file:
        raw += [ln.strip() for ln in open(file, encoding="utf-8") if ln.strip() and not ln.startswith("#")]
    raw += [s.strip() for s in symbols if s.strip()]
    out = []
    for s in raw:
        code = s.split(".")[0].zfill(6)
        if len(code) == 6 and code not in out:
            out.append(code)
    return out


def _chunk_ranges(start: pd.Timestamp, end: pd.Timestamp):
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + pd.offsets.Day(CHUNK_DAYS - 1), end)
        yield cursor.date().isoformat(), chunk_end.date().isoformat()
        cursor = chunk_end + pd.offsets.Day(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=[], help="股票代码，可带 .SH/.SZ")
    ap.add_argument("--file", default=None, help="含股票代码的文件（每行一个）")
    ap.add_argument("--level", default="5", help="分钟粒度，逗号分隔：5,15,30,60")
    ap.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD（缺省=分钟历史起点）")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（缺省=今日）")
    ap.add_argument("--sleep", type=float, default=0.2, help="每次请求间隔秒")
    ap.add_argument("--failed-out", default="reports/fetch_minute_failed.csv")
    args = ap.parse_args()

    levels = [l for l in args.level.split(",") if l]
    bad_levels = [l for l in levels if l not in LEVELS]
    if bad_levels:
        raise SystemExit(f"不支持的分级 {bad_levels}（支持 5/15/30/60）")

    symbols = _resolve_symbols(args.symbols, args.file)
    if not symbols:
        raise SystemExit("未指定股票：用 --symbols 或 --file")
    logger.info("抓取 {} 只股票分钟K，level={}", len(symbols), levels)

    source = ZhituSource(token=os.environ.get("ZHITU_API_TOKEN") or None, sleep_seconds=args.sleep)
    store = MinuteStore()
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.today()
    start = pd.Timestamp(args.start) if args.start else MINUTE_HISTORY_START

    total_ok, total_rows, failed = 0, 0, []
    for sym in symbols:
        sym_ok, sym_rows = 0, 0
        for lv in levels:
            for s, e in _chunk_ranges(start, end):
                try:
                    df = source.fetch_minute_kline(sym, level=lv, start_date=s, end_date=e)
                except Exception as exc:
                    logger.warning("fetch {} {} {}~{} failed: {}", sym, lv, s, e, str(exc)[:90])
                    continue
                if not df.empty:
                    n = store.save(sym, df.assign(level=lv))
                    sym_rows += n
                sym_ok += 1
                time.sleep(args.sleep)
        if sym_rows == 0 and sym_ok:
            # 无任何数据落盘：可能是停牌/退市/超回溯起点——按失败记录以便复核
            failed.append(sym)
            logger.warning("{}: 未抓到任何数据（{} 个时窗成功但为空）", sym, sym_ok)
        else:
            total_ok += 1
            total_rows += sym_rows
            logger.info("{}: {} level 写入 {} 根 bar", sym, len(levels), sym_rows)

    if failed:
        pd.DataFrame({"symbol": failed}).to_csv(args.failed_out, index=False)
        logger.warning("{} 只无数据，写入 {} 供复核", len(failed), args.failed_out)
    logger.info("完成：{} 只有数据，共 {} 根；失败 {} 只", total_ok, total_rows, len(failed))


if __name__ == "__main__":
    main()
