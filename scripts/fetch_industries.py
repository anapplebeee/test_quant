from __future__ import annotations

import argparse
import socket

import pandas as pd
from loguru import logger
from rich.console import Console

from quart.config import PROJECT_ROOT

console = Console()

OUT_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry.parquet"


def fetch_industry_map() -> pd.DataFrame:
    socket.setdefaulttimeout(15)
    import akshare as ak

    sec = ak.sw_index_second_info()
    sec_code_to_first = {}
    if "行业名称" in sec.columns and "上级行业" in sec.columns:
        for _, r in sec.iterrows():
            sec_code_to_first[str(r["行业代码"]).replace(".SI", "")] = r["上级行业"]

    thi = ak.sw_index_third_info()
    rows = []
    total = len(thi)
    for n, (_, r) in enumerate(thi.iterrows(), 1):
        code = str(r["行业代码"]).replace(".SI", "")
        try:
            cons = ak.sw_index_third_cons(symbol=code)
        except Exception as exc:
            logger.warning("cons {} failed: {}", code, str(exc)[:60])
            continue
        if cons is None or cons.empty or "股票代码" not in cons.columns:
            continue
        second_name = r.get("上级行业")
        for sym in cons["股票代码"].astype(str).str.zfill(6).unique():
            rows.append({
                "symbol": sym,
                "ind3": r["行业名称"],
                "ind2": second_name,
                "ind1": sec_code_to_first.get(str(second_name), None),
                "ind3_code": code,
            })
        if n % 25 == 0:
            logger.info("industry fetch progress {}/{}", n, total)

    if not rows:
        raise RuntimeError("no industry constituents fetched")
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if OUT_PATH.exists() and not args.refresh:
        df = pd.read_parquet(OUT_PATH)
        console.print(f"[yellow]cached[/yellow] {df['symbol'].nunique()} symbols -> {OUT_PATH}")
        return
    df = fetch_industry_map()
    df = df.drop_duplicates(subset=["symbol"], keep="first")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PATH, index=False)
    coverage = df["ind1"].notna().mean()
    console.print(f"[green]saved[/green] {df['symbol'].nunique()} symbols | L1 coverage {coverage:.0%} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
