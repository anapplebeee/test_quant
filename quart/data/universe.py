from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root


def get_constituents(index_code: str = "000300") -> list[str]:
    cached = _cache_path(index_code)
    if cached.exists():
        df = pd.read_parquet(cached)
        return df["symbol"].tolist()

    codes = _fetch_from_csindex(index_code) or _fetch_from_sina(index_code)
    if not codes:
        raise RuntimeError(f"failed to fetch constituents for index {index_code}")
    df = pd.DataFrame({"symbol": codes})
    df.to_parquet(cached, index=False)
    logger.info("universe {}: {} constituents cached to {}", index_code, len(codes), cached)
    return codes


def filter_st(codes: list[str], exclude_st: bool = True) -> list[str]:
    if not exclude_st:
        return codes
    from quart.data.source_akshare import fetch_stock_list

    name_map = dict(fetch_stock_list().values.tolist())
    kept = [c for c in codes if "ST" not in name_map.get(c, "").upper() and "退" not in name_map.get(c, "")]
    dropped = len(codes) - len(kept)
    if dropped:
        logger.info("dropped {} ST/delisting names", dropped)
    return kept


def _fetch_from_csindex(index_code: str) -> list[str]:
    try:
        import akshare as ak

        df = ak.index_stock_cons_csindex(symbol=index_code)
        col = next(c for c in df.columns if "成分券代码" in c)
        return sorted(df[col].astype(str).str.zfill(6).unique().tolist())
    except Exception as exc:
        logger.warning("csindex source failed: {}", exc)
        return []


def _fetch_from_sina(index_code: str) -> list[str]:
    try:
        import akshare as ak

        df = ak.index_stock_cons(symbol=index_code)
        return sorted(df["品种代码"].astype(str).str.zfill(6).unique().tolist())
    except Exception as exc:
        logger.warning("sina source failed: {}", exc)
        return []


def _cache_path(index_code: str) -> Path:
    today = dt.date.today().isoformat()
    path = data_root() / "universe" / f"{index_code}_{today}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
