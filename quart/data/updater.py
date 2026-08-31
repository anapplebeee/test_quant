from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from quart.config import load_config
from quart.data.hfq_pins import read_hfq_pins
from quart.data.source_akshare import fetch_daily, fetch_index_daily
from quart.data.store import drop_incomplete_today


def _drift_ratio(store, symbol: str, overlap_start: str, fresh: pd.DataFrame) -> float:
    old = store.load(symbols=[symbol], start=overlap_start)
    if old.empty:
        return 0.0
    last = store.last_date(symbol)
    if last is None:
        return 0.0
    old = old.copy()
    fresh = fresh.copy()
    old["date"] = pd.to_datetime(old["date"], errors="coerce")
    fresh["date"] = pd.to_datetime(fresh["date"], errors="coerce")
    old_side = old[old["date"] <= last][["date", "close"]].rename(columns={"close": "close_old"})
    new_side = fresh[fresh["date"] <= last][["date", "close"]].rename(columns={"close": "close_new"})
    merged = old_side.merge(new_side, on="date").dropna()
    if len(merged) < 3:
        return 0.0
    ratio = (merged["close_new"] / merged["close_old"] - 1.0).abs()
    return float(ratio.median())


class _Throttle:
    """并发安全的限速器。

    目标：每只股票两次请求之间至少间隔 `interval` 秒（防反爬）。
    用「每 symbol 独立记录上次请求时间」实现：不同股票互不阻塞，
    同一股票保证最小间隔。并发下总吞吐 = workers × (1/interval)，
    而不是被一个共享 sleep 串行化成 1/interval。
    """

    def __init__(self, interval: float):
        self._interval = interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, symbol: str) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            last = self._last.get(symbol, 0.0)
            wait = self._interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last[symbol] = time.monotonic()


@dataclass
class _UpdateCounters:
    """并发安全的进度计数器。"""

    ok: int = 0
    empty: int = 0
    failed: int = 0
    refreshed: int = 0
    skipped_blocked: int = 0
    empty_symbols: list[str] = field(default_factory=list)
    failed_symbols: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, status: str, was_refreshed: bool, symbol: str) -> None:
        with self._lock:
            if status == "ok":
                self.ok += 1
            elif status == "empty":
                self.empty += 1
                self.empty_symbols.append(symbol)
            else:
                self.failed += 1
                self.failed_symbols.append(symbol)
            self.refreshed += int(was_refreshed)

    def snapshot(self) -> tuple[int, int, int, int, int]:
        with self._lock:
            return self.ok, self.empty, self.failed, self.refreshed, self.skipped_blocked

    def as_dict(self, total: int) -> dict:
        return {
            "total": total,
            "ok": self.ok,
            "empty": self.empty,
            "failed": self.failed,
            "refreshed": self.refreshed,
            "skipped_blocked": self.skipped_blocked,
            "empty_symbols": sorted(self.empty_symbols),
            "failed_symbols": sorted(self.failed_symbols),
        }


def update_universe_data(
    index_code: str,
    symbols: list[str],
    start: str = "20190101",
    max_names: int | None = None,
    workers: int = 1,
    full_refresh: bool = False,
    force_full: bool | None = None,
) -> dict:
    """更新股票池与基准行情。

    Parameters
    ----------
    workers:
        并发线程数（1-32）。并发下用 ``_Throttle`` 每股票独立限速，
        不同股票互不阻塞，总吞吐 = workers × (1/限速间隔)。
    full_refresh:
        全量刷新：忽略本地最后日期，从 ``start`` 重拉并 replace 覆盖。
    force_full:
        兼容旧参数名（等价于 full_refresh）。只传一个即可，两者同时
        传入时以 full_refresh 为准（显式全量）。
    """
    from quart.data.store import BarStore

    # 兼容：force_full 与 full_refresh 是同一语义，历史调用方可能用旧名
    if force_full is not None:
        full_refresh = bool(full_refresh or force_full)

    cfg = load_config()
    store = BarStore()
    sleep_s = float(cfg["data"]["sleep_seconds"])
    today = dt.date.today().strftime("%Y%m%d")
    counters = _UpdateCounters()
    throttle = _Throttle(sleep_s)

    # hfq 钉住：被 qfq 伪影污染后修复过的股票，增量/全量均用 hfq，防止损坏复发
    hfq_pins = read_hfq_pins()

    # 数据质量阻断：隔离清单中的符号（物理不可能跳变 = 复权/源数据坏）不再更新，
    # 防止坏数据在每次增量更新中持续扩散；恢复需人工复核后从清单移除
    from quart.data.quality import load_blocklist

    blocked = load_blocklist()

    targets = list(dict.fromkeys(symbols[:max_names] if max_names else symbols))
    workers = max(1, min(int(workers), 32))

    def update_symbol(symbol: str) -> tuple[str, bool, str]:
        # 限速放在 worker 内部：每只股票请求前先等够间隔，
        # 不阻塞主线程调度，也不串行化其他线程。
        throttle(symbol)
        try:
            adjust = "hfq" if symbol in hfq_pins else cfg["data"]["adjust"]
            first = store.first_date(symbol)
            needs_full = first is None or (start and pd.Timestamp(start) < first)

            # 全量模式（--full/--full-refresh）：跳过增量/漂移检测，整史重拉 + replace
            if full_refresh or needs_full:
                df = fetch_daily(symbol, start, today, adjust=adjust)
                df = drop_incomplete_today(df)  # 盘中更新剔除当日未收盘 partial bar，防未来函数
                if df.empty:
                    return "empty", False, symbol
                else:
                    store.save(df, replace=True)
                    return "ok", full_refresh, symbol
            else:
                last = store.last_date(symbol)
                overlap_start = (last - pd.Timedelta(days=20)).strftime("%Y%m%d")
                next_day = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
                df = fetch_daily(symbol, overlap_start, today, adjust=adjust)
                df = drop_incomplete_today(df)  # 盘中更新剔除当日未收盘 partial bar
                if df.empty:
                    return "empty", False, symbol
                elif _drift_ratio(store, symbol, overlap_start, df) > 0.002:
                    logger.info("{} adjustment drift detected, full refresh", symbol)
                    full = fetch_daily(symbol, start, today, adjust=adjust)
                    full = drop_incomplete_today(full)
                    if full.empty:
                        return "empty", False, symbol
                    store.save(full, replace=True)
                    return "ok", True, symbol
                elif next_day > today:
                    return "ok", False, symbol
                else:
                    store.save(df[df["date"] >= pd.Timestamp(next_day)])
                    return "ok", False, symbol
        except Exception as exc:
            logger.warning("update {} failed: {}", symbol, exc)
            return "failed", False, symbol

    def log_progress(n: int) -> None:
        ok, empty, failed, refreshed, skipped = counters.snapshot()
        logger.info(
            "progress {}/{}, ok={} empty={} failed={} refreshed={} skipped_blocked={}",
            n, len(targets), ok, empty, failed, refreshed, skipped,
        )

    # 预处理：隔离清单中的符号直接跳过（不入并发池，避免空转）
    active_targets = []
    for symbol in targets:
        if symbol in blocked:
            with counters._lock:
                counters.skipped_blocked += 1
            continue
        active_targets.append(symbol)

    if workers == 1:
        for n, symbol in enumerate(active_targets, 1):
            counters.record(*update_symbol(symbol))
            if n % 50 == 0:
                log_progress(n)
    else:
        logger.info("parallel data update enabled: {} workers", workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bar-update") as executor:
            futures = {executor.submit(update_symbol, symbol): symbol for symbol in active_targets}
            for n, future in enumerate(as_completed(futures), 1):
                try:
                    counters.record(*future.result())
                except Exception as exc:
                    symbol = futures[future]
                    with counters._lock:
                        counters.failed += 1
                        counters.failed_symbols.append(symbol)
                    logger.warning("update {} worker failed: {}", symbol, exc)
                if n % 50 == 0 or n == len(active_targets):
                    log_progress(n)

    bench_df = fetch_index_daily(index_code, start, today)
    bench_df = drop_incomplete_today(bench_df)
    if not bench_df.empty:
        store.save(bench_df, replace=full_refresh)
    return counters.as_dict(len(active_targets))
