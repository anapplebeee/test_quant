from __future__ import annotations

import datetime as dt

import pandas as pd
from loguru import logger

from quart.config import load_config
from quart.data.source_akshare import fetch_daily, fetch_index_daily, polite_sleep


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
) -> dict:
    from quart.data.store import BarStore

    cfg = load_config()
    store = BarStore()
    sleep_s = float(cfg["data"]["sleep_seconds"])
    today = dt.date.today().strftime("%Y%m%d")
    ok, empty, failed, refreshed = 0, 0, 0, 0

    targets = list(symbols[:max_names] if max_names else symbols)
    for n, symbol in enumerate(targets, 1):
        try:
            first = store.first_date(symbol)
            needs_full = first is None or (start and pd.Timestamp(start) < first)

            if needs_full:
                df = fetch_daily(symbol, start, today, adjust=cfg["data"]["adjust"])
                if df.empty:
                    empty += 1
                else:
                    store.save(df, replace=True)
                    ok += 1
            else:
                last = store.last_date(symbol)
                overlap_start = (last - pd.Timedelta(days=20)).strftime("%Y%m%d")
                next_day = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
                df = fetch_daily(symbol, overlap_start, today, adjust=cfg["data"]["adjust"])
                if df.empty:
                    empty += 1
                elif _drift_ratio(store, symbol, overlap_start, df) > 0.002:
                    logger.info("{} adjustment drift detected, full refresh", symbol)
                    full = fetch_daily(symbol, start, today, adjust=cfg["data"]["adjust"])
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
    if not bench_df.empty:
        store.save(bench_df)
    return {"total": len(targets), "ok": ok, "empty": empty, "failed": failed, "refreshed": refreshed}
