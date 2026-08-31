from __future__ import annotations

import datetime as dt

import pandas as pd
from loguru import logger

from quart.config import load_config
from quart.data.hfq_pins import read_hfq_pins
from quart.data.source_akshare import fetch_daily, fetch_index_daily, polite_sleep
from quart.data.store import drop_incomplete_today


def _drift_ratio(store, symbol: str, overlap_start: str, fresh: pd.DataFrame) -> float:
    old = store.load(symbols=[symbol], start=overlap_start)
    if old.empty:
        return 0.0
    last = store.last_date(symbol)
    old_side = old[old["date"] <= last][["date", "close"]].rename(columns={"close": "close_old"})
    new_side = fresh[fresh["date"] <= last][["date", "close"]].rename(columns={"close": "close_new"})
    merged = old_side.merge(new_side, on="date").dropna()
    if len(merged) < 3:
        return 0.0
    ratio = (merged["close_new"] / merged["close_old"] - 1.0).abs()
    return float(ratio.median())


def update_universe_data(
    index_code: str,
    symbols: list[str],
    start: str = "20190101",
    max_names: int | None = None,
    force_full: bool = False,
) -> dict:
    """更新股票池与基准行情。

    ``force_full`` 为显式全量刷新：忽略本地最后日期，从 ``start`` 重拉并
    覆盖远端实际返回的年份分区。远端返回空表时保留旧数据，避免瞬时断网
    或限流把本地有效历史清空。
    """
    from quart.data.store import BarStore

    cfg = load_config()
    store = BarStore()
    sleep_s = float(cfg["data"]["sleep_seconds"])
    today = dt.date.today().strftime("%Y%m%d")
    ok, empty, failed, refreshed = 0, 0, 0, 0

    # hfq 钉住：被 qfq 伪影污染后修复过的股票，增量/全量均用 hfq，防止损坏复发
    hfq_pins = read_hfq_pins()

    # 数据质量阻断：隔离清单中的符号（物理不可能跳变 = 复权/源数据坏）不再更新，
    # 防止坏数据在每次增量更新中持续扩散；恢复需人工复核后从清单移除
    from quart.data.quality import load_blocklist

    blocked = load_blocklist()
    skipped_blocked = 0

    targets = list(symbols[:max_names] if max_names else symbols)
    for n, symbol in enumerate(targets, 1):
        if symbol in blocked:
            skipped_blocked += 1
            continue
        try:
            adjust = "hfq" if symbol in hfq_pins else cfg["data"]["adjust"]
            first = store.first_date(symbol)
            needs_full = force_full or first is None or (start and pd.Timestamp(start) < first)

            if needs_full:
                df = fetch_daily(symbol, start, today, adjust=adjust)
                df = drop_incomplete_today(df)  # 盘中更新剔除当日未收盘 partial bar，防未来函数
                if df.empty:
                    empty += 1
                else:
                    store.save(df, replace=True)
                    if force_full:
                        refreshed += 1
                    ok += 1
            else:
                last = store.last_date(symbol)
                overlap_start = (last - pd.Timedelta(days=20)).strftime("%Y%m%d")
                next_day = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
                df = fetch_daily(symbol, overlap_start, today, adjust=adjust)
                df = drop_incomplete_today(df)  # 盘中更新剔除当日未收盘 partial bar
                if df.empty:
                    empty += 1
                elif _drift_ratio(store, symbol, overlap_start, df) > 0.002:
                    logger.info("{} adjustment drift detected, full refresh", symbol)
                    full = fetch_daily(symbol, start, today, adjust=adjust)
                    full = drop_incomplete_today(full)
                    store.save(full, replace=True)
                    refreshed += 1
                    ok += 1
                elif next_day > today:
                    ok += 1
                else:
                    store.save(df[df["date"] >= pd.Timestamp(next_day)])
                    ok += 1
        except Exception as exc:
            failed += 1
            logger.warning("update {} failed: {}", symbol, exc)
        finally:
            polite_sleep(sleep_s)
        if n % 50 == 0:
            logger.info("progress {}/{}, ok={} empty={} failed={} refreshed={}", n, len(targets), ok, empty, failed, refreshed)

    bench_df = fetch_index_daily(index_code, start, today)
    bench_df = drop_incomplete_today(bench_df)
    if not bench_df.empty:
        store.save(bench_df, replace=force_full)
    return {
        "total": len(targets),
        "ok": ok,
        "empty": empty,
        "failed": failed,
        "refreshed": refreshed,
        "full_refresh": force_full,
    }
