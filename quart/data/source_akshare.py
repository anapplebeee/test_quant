from __future__ import annotations

import random
import socket
import threading
import time

import numpy as np
import pandas as pd
from loguru import logger

socket.setdefaulttimeout(20)

#: 网络层可重试错误集合（requests 连接断开/超时 + 底层 socket/超时错误）。
#: 东财接口反爬/波动时表现为 RemoteDisconnected，会被 requests 包装成
#: requests.exceptions.ConnectionError，因此不能只依赖内建 ConnectionError。
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    ConnectionError,
)
try:
    import requests

    _RETRYABLE_EXC += (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )
except ImportError:  # pragma: no cover - requests 必装（akshare 依赖）
    pass

OHLC_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}

TX_BAR_COLUMNS = ["date", "open", "close", "high", "low", "volume", "turnover", "amount"]

_EMPTY = pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume", "amount"])


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _retry(fn, retries: int = 3, base_delay: float = 1.0):
    """指数退避 + 抖动重试；仅对网络层错误重试，其余异常直接抛出。"""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 统一在下方判定是否可重试
            if not isinstance(exc, _RETRYABLE_EXC) or attempt == retries:
                raise
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.debug("retry {}/{} after {:.1f}s: {}", attempt, retries, delay, str(exc)[:80])
            polite_sleep(delay)
    raise last_exc  # pragma: no cover - retries >= 1 时逻辑上不可达


class _CircuitBreaker:
    """进程内熔断器：连续失败 N 次后短路 open_seconds 秒；到期自动 half-open 放行一次探针。

    - open 期间请求直接短路，不再打东财（避免批量更新在限流期白等重试）。
    - half-open 探针成功 -> success() 复位；失败 -> failure() 立即重新熔断。
    - 仅网络层错误计数，业务错误（如非法代码）不影响熔断状态。
    """

    def __init__(self, failure_threshold: int = 3, open_seconds: float = 300.0):
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is not None:
                if time.monotonic() - self._opened_at >= self._open_seconds:
                    self._opened_at = None  # half-open：放行一次探针
                    logger.debug("breaker half-open, allow probe")
                    return True
                return False
            return True

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                logger.warning(
                    "breaker OPEN: {} consecutive network failures, skip for {:.1f}s",
                    self._failures,
                    self._open_seconds,
                )


def to_tx_symbol(symbol: str) -> str:
    code = symbol.removeprefix("IDX")
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


def to_tx_index_symbol(code: str) -> str:
    prefix = "sz" if code.startswith("399") else "sh"
    return f"{prefix}{code}"


def fetch_stock_list() -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.stock_info_a_code_name()
        return df.rename(columns={"code": "symbol", "name": "name"})[["symbol", "name"]]
    except Exception as exc:
        logger.warning("stock_info_a_code_name failed: {}", exc)
    try:
        df = ak.stock_zh_a_spot_em()
        return df[["代码", "名称"]].rename(columns={"代码": "symbol", "名称": "name"})
    except Exception as exc:
        logger.warning("stock_zh_a_spot_em failed: {}", exc)
    return pd.DataFrame(columns=["symbol", "name"])


#: 数据源优先级：腾讯主源（稳定、无封禁），东财兜底（历史更全、有周/月线）。
#: 实测（2026-08-28）：腾讯 qfq 与东财 qfq 口径基本一致（除权日附近瞬时差异 <=0.22%，
#: 低于 updater._drift_ratio 的 0.2% 全量刷新阈值），历史深度 2005 年起，字段含换手率/成交额。
def fetch_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> pd.DataFrame:
    # 2026-08-31 审查修复：腾讯异常（列名变更/网络故障）也必须走东财兜底，
    # 否则单只股票会永久拉取失败且被记 failed。
    try:
        df = _fetch_daily_tencent(symbol, start_date, end_date, adjust)
    except Exception as exc:
        logger.debug("tencent daily {} failed, fallback to eastmoney: {}", symbol, str(exc)[:80])
        df = _EMPTY.copy()
    if not df.empty:
        return df
    return _fetch_daily_eastmoney(symbol, start_date, end_date, adjust)


def fetch_index_daily(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        df = _fetch_index_tencent(code)
    except Exception as exc:
        logger.debug("tencent index {} failed, fallback to eastmoney: {}", code, str(exc)[:80])
        df = _EMPTY.copy()
    if not df.empty:
        return df
    return _fetch_index_eastmoney(code, start_date, end_date)


#: 东财源（个股 + 指数共用同一 host，共用一个熔断器）。
#: 批量更新期间一旦东财持续断连，熔断后直接短路，全量切腾讯兜底，不再浪费重试等待。
_EASTMONEY_BREAKER = _CircuitBreaker()


def _fetch_daily_eastmoney(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    if not _EASTMONEY_BREAKER.allow():
        logger.debug("eastmoney breaker open, skip {}", symbol)
        return _EMPTY.copy()
    try:
        raw = _retry(
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        )
    except Exception as exc:
        logger.debug("eastmoney daily {} failed: {}", symbol, str(exc)[:80])
        if isinstance(exc, _RETRYABLE_EXC):
            _EASTMONEY_BREAKER.failure()
        return _EMPTY.copy()
    _EASTMONEY_BREAKER.success()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    df = raw.rename(columns=OHLC_MAP)
    df["symbol"] = symbol
    keep = [c for c in list(OHLC_MAP.values()) + ["symbol"] if c in df.columns]
    return df[keep]


def _align_schema(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for col in ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]:
        if col == "symbol":
            out[col] = symbol
        elif col == "date":
            out[col] = pd.to_datetime(df[col], errors="coerce") if col in df.columns else pd.NaT
        else:
            out[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else float("nan")
    return out


def _normalize_volume_unit(df: pd.DataFrame) -> pd.DataFrame:
    """统一腾讯源成交量为「手」（与东财/引擎口径一致）。

    2026-08-31 审查修复：`stock_zh_a_hist_tx` 对 000 开头（老深主板）返回
    volume 单位=手，其余（002/300/6xx/688）返回单位=股，同一横截面相差 100 倍，
    导致 volume 类因子（net_flow20、volume_ratio20 等）跨股票比较失真。
    用 amount / (close × volume) 的中位比值自动判定：≈1 → 股 → ÷100 转手；
    ≈100 → 已是手 → 保持不变。
    """
    if df is None or df.empty or "volume" not in df.columns:
        return df
    v = df["volume"].replace(0, np.nan)
    px = df["close"].replace(0, np.nan)
    amt = df["amount"]
    ratio = (amt / (px * v)).median()
    if np.isnan(ratio):
        return df
    if 0.5 <= ratio <= 2.0:  # volume 单位是股 → 转手
        out = df.copy()
        out["volume"] = out["volume"] / 100.0
        return out
    return df


def _fetch_daily_tencent(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = _retry(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=to_tx_symbol(symbol),
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        )
    except Exception as exc:
        logger.debug("tencent daily {} failed: {}", symbol, str(exc)[:80])
        return _EMPTY.copy()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    return _normalize_volume_unit(_align_schema(raw[TX_BAR_COLUMNS].copy(), symbol))


def _fetch_index_eastmoney(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak

    if not _EASTMONEY_BREAKER.allow():
        logger.debug("eastmoney breaker open, skip index {}", code)
        return _EMPTY.copy()
    try:
        raw = _retry(lambda: ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date))
    except Exception as exc:
        logger.debug("eastmoney index {} failed: {}", code, str(exc)[:80])
        if isinstance(exc, _RETRYABLE_EXC):
            _EASTMONEY_BREAKER.failure()
        return _EMPTY.copy()
    _EASTMONEY_BREAKER.success()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    df = raw.rename(columns=OHLC_MAP)
    df["symbol"] = f"IDX{code}"
    keep = [c for c in list(OHLC_MAP.values()) + ["symbol"] if c in df.columns]
    return df[keep]


def _fetch_index_tencent(code: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = _retry(lambda: ak.stock_zh_index_daily_tx(symbol=to_tx_index_symbol(code)))
    except Exception as exc:
        logger.debug("tencent index {} failed: {}", code, str(exc)[:80])
        return _EMPTY.copy()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    return _align_schema(raw.copy(), f"IDX{code}")
