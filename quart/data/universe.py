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


# 科创板（含 CDR：688/689）与创业板（300/301）代码前缀
STAR_PREFIXES = ("688", "689")
CHINEXT_PREFIXES = ("300", "301")


def filter_boards(
    codes: list[str],
    exclude_star: bool = True,
    exclude_chinext: bool = True,
) -> list[str]:
    """按代码前缀剥离科创板/创业板。

    科创板(STAR): 688 / 689(CDR)；创业板(ChiNext): 300 / 301。
    这些板块涨跌停幅度为 20%（与主板 10% 不同），且交易门槛/波动特征
    差异大，模拟盘常需排除。
    """
    dropped_star = dropped_chinext = 0
    kept: list[str] = []
    for c in codes:
        code = str(c)
        if exclude_star and code.startswith(STAR_PREFIXES):
            dropped_star += 1
            continue
        if exclude_chinext and code.startswith(CHINEXT_PREFIXES):
            dropped_chinext += 1
            continue
        kept.append(c)
    if dropped_star:
        logger.info("dropped {} 科创板 names", dropped_star)
    if dropped_chinext:
        logger.info("dropped {} 创业板 names", dropped_chinext)
    return kept


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


def filter_for_simulation(
    bars: pd.DataFrame,
    exclude_star: bool = True,
    exclude_chinext: bool = True,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """在模拟（回测/信号）路径上对行情截面统一剔除板块与 ST。

    数据下载层保留全市场原始数据，仅在模拟时按配置过滤，避免污染底层数据。
    """
    if bars.empty:
        return bars
    codes = bars["symbol"].unique().tolist()
    codes = filter_boards(codes, exclude_star=exclude_star, exclude_chinext=exclude_chinext)
    if exclude_st:
        try:
            codes = filter_st(codes)
        except Exception as exc:  # 取不到股票名列表时降级为只做板块过滤
            logger.warning("ST filter skipped in simulation: {}", exc)
    return bars[bars["symbol"].isin(codes)].copy()


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
