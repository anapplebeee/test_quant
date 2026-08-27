from __future__ import annotations

import socket
import time

import pandas as pd
from loguru import logger

socket.setdefaulttimeout(20)

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


def fetch_daily(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> pd.DataFrame:
    df = _fetch_daily_eastmoney(symbol, start_date, end_date, adjust)
    if not df.empty:
        return df
    return _fetch_daily_tencent(symbol, start_date, end_date, adjust)


def fetch_index_daily(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = _fetch_index_eastmoney(code, start_date, end_date)
    if not df.empty:
        return df
    return _fetch_index_tencent(code)


def _fetch_daily_eastmoney(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    except Exception as exc:
        logger.debug("eastmoney daily {} failed: {}", symbol, str(exc)[:80])
        return _EMPTY.copy()
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


def _fetch_daily_tencent(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = ak.stock_zh_a_hist_tx(
            symbol=to_tx_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    except Exception as exc:
        logger.debug("tencent daily {} failed: {}", symbol, str(exc)[:80])
        return _EMPTY.copy()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    return _align_schema(raw[TX_BAR_COLUMNS].copy(), symbol)


def _fetch_index_eastmoney(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date)
    except Exception as exc:
        logger.debug("eastmoney index {} failed: {}", code, str(exc)[:80])
        return _EMPTY.copy()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    df = raw.rename(columns=OHLC_MAP)
    df["symbol"] = f"IDX{code}"
    keep = [c for c in list(OHLC_MAP.values()) + ["symbol"] if c in df.columns]
    return df[keep]


def _fetch_index_tencent(code: str) -> pd.DataFrame:
    import akshare as ak

    try:
        raw = ak.stock_zh_index_daily_tx(symbol=to_tx_index_symbol(code))
    except Exception as exc:
        logger.debug("tencent index {} failed: {}", code, str(exc)[:80])
        return _EMPTY.copy()
    if raw is None or raw.empty:
        return _EMPTY.copy()
    return _align_schema(raw.copy(), f"IDX{code}")
