"""财报 PIT 全市场抓取（RESEARCH-002 §8-1，P0）。

按报告期抓取东财业绩报表（ak.stock_yjbb_em），单次请求即覆盖全市场
（含此后退市的样本），记录公告时间 / 供应商到达时间 / 修订版本。

用法：
    uv run python scripts/fetch_financials_fullmarket.py              # 抓取+合并落盘
    uv run python scripts/fetch_financials_fullmarket.py --since 2022  # 只补 2022 起报告期
    uv run python scripts/fetch_financials_fullmarket.py --report-only # 仅统计覆盖率

特性：
- 逐期缓存（data/factors/pit_period_cache/），中断后重跑自动续传；
- 值变化检测 → revision 递增，旧版写入 financials_revisions.parquet；
- 旧版新浪逐股数据（无公告时间）保留为 revision=0 的补充行；
- --retry 内置指数退避，适应东财限流。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime as dt
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import data_root
from quart.data.financials_pit import (
    FIN_CACHE_DIR,
    FIN_MAIN_PATH,
    FIN_PIT_DIR,
    FIN_REVISIONS_PATH,
    coverage_report,
    merge_flash_announcements,
    merge_revisions,
    normalize_yjbb,
    normalize_yjkb,
)

console = Console()


def _periods_since(year: int) -> list[str]:
    """year 年 Q1 起至最近已完成报告期的季度末列表（YYYYMMDD）。"""
    today = dt.now()
    out = []
    for y in range(year, today.year + 1):
        for md in ("0331", "0630", "0930", "1231"):
            p = f"{y}{md}"
            if pd.Timestamp(p) <= pd.Timestamp(today.date()):
                out.append(p)
    return out


def _fetch_period(period: str, retries: int, sleep_s: float) -> pd.DataFrame | None:
    """带缓存与重试的单期抓取。"""
    cache = FIN_CACHE_DIR / f"yjbb_{period}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    import akshare as ak

    for attempt in range(1, retries + 1):
        try:
            raw = ak.stock_yjbb_em(date=period)
            if raw is None or raw.empty:
                logger.warning("period {} empty response", period)
                return None
            fetched_at = pd.Timestamp.now()
            snap = normalize_yjbb(raw, period, fetched_at)
            FIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            snap.to_parquet(cache, index=False)
            time.sleep(sleep_s)
            return snap
        except Exception as exc:  # 网络层重试
            wait = sleep_s * (2 ** (attempt - 1))
            logger.warning("period {} attempt {}/{} failed: {} -> retry in {:.0f}s",
                           period, attempt, retries, exc, wait)
            time.sleep(wait)
    return None


def _fetch_flash(period: str, retries: int, sleep_s: float) -> pd.DataFrame | None:
    """业绩快报一期（披露更早，用于重建首次公告时间）。"""
    cache = FIN_CACHE_DIR / f"yjkb_{period}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    import akshare as ak

    for attempt in range(1, retries + 1):
        try:
            raw = ak.stock_yjkb_em(date=period)
            if raw is None or raw.empty:
                return None
            snap = normalize_yjkb(raw, period, pd.Timestamp.now())
            FIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            snap.to_parquet(cache, index=False)
            time.sleep(sleep_s)
            return snap
        except Exception as exc:  # 网络层重试
            logger.warning("yjkb {} attempt {} failed: {}", period, attempt, exc)
            time.sleep(sleep_s * (2 ** (attempt - 1)))
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="财报 PIT 全市场抓取（按报告期）")
    parser.add_argument("--since", type=int, default=2018, help="起始报告期年份")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--report-only", action="store_true", help="仅输出覆盖率报告")
    args = parser.parse_args()

    if args.report_only:
        if not FIN_MAIN_PATH.exists():
            raise SystemExit(f"main file missing: {FIN_MAIN_PATH}")
        fin = pd.read_parquet(FIN_MAIN_PATH)
        names_path = data_root() / "stock_names.parquet"
        all_syms = (
            pd.read_parquet(names_path)["code"].astype(str).str.zfill(6).tolist()
            if names_path.exists() else fin["symbol"].unique().tolist()
        )
        rep = coverage_report(fin, all_syms)
        t = Table(title="财报 PIT 覆盖率")
        for c in rep.columns:
            t.add_column(c, justify="right" if c != "report_period" else "left")
        for _, r in rep.iterrows():
            t.add_row(*[f"{r[c]:.1%}" if c == "coverage_pct" else str(r[c]) for c in rep.columns])
        console.print(t)
        overall = fin["symbol"].nunique()
        console.print(f"总符号数: {overall} / 全市场 {len(all_syms)} "
                      f"= {overall / max(len(all_syms), 1):.1%}")
        return

    periods = _periods_since(args.since)
    console.print(f"抓取报告期: {periods[0]} ~ {periods[-1]}（共 {len(periods)} 期）")

    snaps: list[pd.DataFrame] = []
    for i, period in enumerate(periods, 1):
        snap = _fetch_period(period, args.retries, args.sleep)
        if snap is not None:
            snaps.append(snap)
        if i % 4 == 0 or i == len(periods):
            console.print(f"  progress {i}/{len(periods)}")
    if not snaps:
        raise SystemExit("所有报告期抓取失败")

    snapshot = pd.concat(snaps, ignore_index=True)
    snapshot["revision"] = 0  # merge_revisions 内赋值

    # 业绩快报：重建首次公告时间（快报早于正式报告且含实际初步数值）
    flashes = [f for p in periods if (f := _fetch_flash(p, args.retries, args.sleep)) is not None]
    if flashes:
        snapshot = merge_flash_announcements(snapshot, pd.concat(flashes, ignore_index=True))
        console.print(f"flash announcements merged: {sum(int(f['flash_announce_date'].notna().sum()) for f in flashes)}")

    main = pd.read_parquet(FIN_MAIN_PATH) if FIN_MAIN_PATH.exists() else pd.DataFrame()
    revisions = (
        pd.read_parquet(FIN_REVISIONS_PATH)
        if FIN_REVISIONS_PATH.exists() else pd.DataFrame()
    )
    # 旧新浪逐股数据：无公告时间，标记来源，作为补充行保留
    if not main.empty and "source" not in main.columns:
        main["source"] = "sina_indicator"
        main["available_at"] = pd.NaT
        main["announcement_date"] = pd.to_datetime(main.get("announcement_date"), errors="coerce")

    new_main, new_revisions = merge_revisions(main, revisions, snapshot)
    new_main["date"] = pd.to_datetime(new_main["date"])
    FIN_PIT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FIN_MAIN_PATH.with_suffix(".tmp")
    new_main.to_parquet(tmp, index=False)
    tmp.replace(FIN_MAIN_PATH)
    if not new_revisions.empty:
        new_revisions.to_parquet(FIN_REVISIONS_PATH, index=False)

    names_path = data_root() / "stock_names.parquet"
    all_syms = (
        pd.read_parquet(names_path)["code"].astype(str).str.zfill(6).tolist()
        if names_path.exists() else new_main["symbol"].unique().tolist()
    )
    rep = coverage_report(new_main, all_syms)
    console.print(rep.tail(8).to_string(index=False))
    n_delisted = len(set(new_main["symbol"]) - set(all_syms))
    console.print(
        f"[green]saved {len(new_main)} rows / {new_main['symbol'].nunique()} symbols "
        f"(含退市样本 {n_delisted}); revisions logged: {len(new_revisions)}[/green]"
    )
    with_ann = new_main["announcement_date"].notna().mean()
    console.print(f"公告时间覆盖率: {with_ann:.1%}")


if __name__ == "__main__":
    main()
