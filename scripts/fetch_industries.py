from __future__ import annotations

import argparse
import socket
import time
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console

from quart.config import PROJECT_ROOT

console = Console()

OUT_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry.parquet"
PARTIAL_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry_partial.parquet"
FAILED_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry_failed.txt"


def _fetch_one(ak, code: str) -> pd.DataFrame | None:
    try:
        cons = ak.sw_index_third_cons(symbol=code)
    except Exception as exc:
        logger.warning("cons {} failed: {}", code, str(exc)[:60])
        return None
    if cons is None or cons.empty or "股票代码" not in cons.columns:
        return None
    return cons


def fetch_industry_map() -> pd.DataFrame:
    socket.setdefaulttimeout(12)
    import akshare as ak

    sec = ak.sw_index_second_info()
    sec_code_to_first = {
        str(r["行业代码"]).replace(".SI", ""): r["上级行业"]
        for _, r in sec.iterrows()
    }

    thi = ak.sw_index_third_info()
    done_codes = set()
    frames = []
    if PARTIAL_PATH.exists():
        prev = pd.read_parquet(PARTIAL_PATH)
        frames.append(prev)
        done_codes = set(prev["ind3_code"].astype(str).unique())
        logger.info("resuming, {} industries already cached", len(done_codes))

    failed_before = set()
    if FAILED_PATH.exists():
        failed_before = set(Path(FAILED_PATH).read_text().split())

    new_rows = []
    new_failed = set()
    total = len(thi)
    processed = 0
    pending_flush = 0
    for n, (_, r) in enumerate(thi.iterrows(), 1):
        code = str(r["行业代码"]).replace(".SI", "")
        if code.startswith("85"):
            continue
        if code in done_codes or code in failed_before:
            continue
        cons = _fetch_one(ak, code)
        processed += 1
        if cons is None:
            new_failed.add(code)
            continue
        second_name = r.get("上级行业")
        for sym in cons["股票代码"].astype(str).str.zfill(6).unique():
            new_rows.append({
                "symbol": sym,
                "ind3": r["行业名称"],
                "ind2": second_name,
                "ind1": sec_code_to_first.get(str(second_name), None),
                "ind3_code": code,
            })
        if processed % 20 == 0:
            logger.info("{}/{} fetched-ok={} fail={}", n, total, processed - len(new_failed), len(new_failed))

    if new_rows:
        base = pd.DataFrame(new_rows)
        if PARTIAL_PATH.exists():
            prev = pd.read_parquet(PARTIAL_PATH)
            merged = pd.concat([prev[base.columns], base], ignore_index=True)
        else:
            merged = base
        merged.drop_duplicates(subset=["symbol"], keep="last").to_parquet(PARTIAL_PATH, index=False)

    all_failed = sorted(failed_before | new_failed)
    Path(FAILED_PATH).write_text("\n".join(all_failed))

    if not frames and not new_rows:
        raise RuntimeError("no industry constituents fetched")
    result = pd.concat(frames + ([pd.DataFrame(new_rows)] if new_rows else []), ignore_index=True)
    return result.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if OUT_PATH.exists() and not args.refresh:
        df = pd.read_parquet(OUT_PATH)
        console.print(f"[yellow]cached[/yellow] {df['symbol'].nunique()} symbols -> {OUT_PATH}")
        return
    df = fetch_industry_map()
    df.to_parquet(PARTIAL_PATH, index=False)
    coverage = df["ind1"].notna().mean()
    df.to_parquet(OUT_PATH, index=False)
    console.print(f"[green]saved[/green] {df['symbol'].nunique()} symbols | L1 coverage {coverage:.0%} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
