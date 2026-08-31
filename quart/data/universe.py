from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root


def get_constituents(
    index_code: str = "000300",
    as_of: str | pd.Timestamp | None = None,
    strict_pit: bool = False,
) -> list[str]:
    """获取成分股。

    Parameters
    ----------
    as_of:
        查询某**历史日期**的成分股（消除前视偏差）。
        传入后优先查 PIT 变更记录（`universe_history` 模块）。
        找不到记录时：默认回退到当前快照并打 WARNING（回测数字含前视偏差）；
        `strict_pit=True` 则直接抛错。
    strict_pit:
        True 时拒绝"无历史就回退快照"的静默降级。

    为什么重要
    ----------
    用今天的成分股跑 2020 年的回测 = 前视偏差。A 股实测量级 3-8pp/yr，
    比本项目已修复的退市股偏差（-2.0~-2.6pp/yr）更大。
    """
    if as_of is not None:
        from quart.data.universe_history import constituents_at

        pit = constituents_at(index_code, as_of)
        # None = 无历史记录；[] = 有历史但该日无成分（如指数尚未成立）。
        # 后者必须返回空股票池，不能回退当前快照——否则会在指数未成立的
        # 早期日期注入今日成分股（前视偏差）。
        # 2026-08-31 审查修复：旧代码 `if pit:` 把 [] 也判假，静默回退快照。
        if pit is not None:
            return pit
        msg = (
            f"{index_code} 无 {pd.Timestamp(as_of).date()} 的 PIT 成分股记录，"
            f"回退到当前快照（回测结果含前视偏差）。"
            f"运行 scripts/build_universe_history.py 构建历史。"
        )
        if strict_pit:
            raise RuntimeError(msg)
        logger.warning(msg)

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


def get_list_dates(force_refresh: bool = False) -> pd.Series:
    """全市场上市首日表（symbol → first bar date），本地缓存 data/universe/list_dates.parquet。

    用于次新股过滤（前视缓解）：回测窗口内才出现的首根 bar 不代表真实上市日，
    必须查全历史首日；无记录的 symbol 回退用窗口内首日。
    """
    from quart.data.store import BarStore

    cache = data_root() / "universe" / "list_dates.parquet"
    if cache.exists() and not force_refresh:
        df = pd.read_parquet(cache)
        return df.set_index("symbol")["first_date"]

    store = BarStore()
    rows = []
    for sym in store.symbols():
        fd = store.first_date(sym)
        if fd is not None:
            rows.append({"symbol": sym, "first_date": pd.Timestamp(fd)})
    df = pd.DataFrame(rows)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    logger.info("list_dates cached: {} symbols -> {}", len(df), cache)
    return df.set_index("symbol")["first_date"]


def filter_for_simulation(
    bars: pd.DataFrame,
    exclude_star: bool = True,
    exclude_chinext: bool = True,
    exclude_st: bool = True,
    min_list_days: int = 0,
) -> pd.DataFrame:
    """在模拟（回测/信号）路径上对行情截面统一剔除板块与 ST。

    数据下载层保留全市场原始数据，仅在模拟时按配置过滤，避免污染底层数据。

    min_list_days > 0 时剔除上市不满 N 个自然日的次新股行：次新股无涨跌幅
    限制、筹码结构特殊，且部分回测起始日才“出现”的股票实为早已上市
    （历史数据缺失），须用全历史首日而非窗口内首日判断。
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
    out = bars[bars["symbol"].isin(codes)]
    if min_list_days > 0 and not out.empty:
        try:
            ld = get_list_dates()
        except Exception as exc:
            logger.warning("list_dates unavailable, skip min-list-days filter: {}", exc)
            return out.copy()
        first_visible = out.groupby("symbol")["date"].transform("min")
        list_ref = out["symbol"].map(ld)
        list_ref = list_ref.fillna(first_visible)
        keep = (out["date"] - list_ref).dt.days >= min_list_days
        dropped = int((~keep).sum())
        if dropped:
            logger.info("dropped {} bars of stocks listed < {}d", dropped, min_list_days)
        out = out[keep]
    return out.copy()


def filter_for_pit_universe(
    bars: pd.DataFrame,
    index_code: str = "000300",
    require_complete: bool = True,
) -> pd.DataFrame:
    """按交易日应用 Point-in-Time 成分股历史。

    ``get_constituents()`` 的当前快照回退适合探索，但不能用于正式回测。
    本函数只读取 ``universe_history`` 的生效区间，并在历史缺失或覆盖不完整
    时阻断，避免把当前成分股静默套到过去日期。
    """
    if bars.empty:
        return bars.copy()
    from quart.data.universe_history import load_history

    hist = load_history(index_code)
    if hist is None or hist.empty:
        raise RuntimeError(
            f"{index_code} 缺少 PIT 成分股历史；正式回测被阻断。"
            "请先运行 scripts/build_universe_history.py，或显式使用 exploratory 模式。"
        )
    hist = hist.copy()
    hist["symbol"] = hist["symbol"].astype(str).str.zfill(6)
    hist["in_date"] = pd.to_datetime(hist["in_date"])
    hist["out_date"] = pd.to_datetime(hist["out_date"]).fillna(pd.Timestamp("2262-01-01"))
    bars_dates = pd.to_datetime(bars["date"])
    unique_dates = pd.DatetimeIndex(bars_dates.drop_duplicates().sort_values())
    active_by_date: dict[pd.Timestamp, set[str]] = {}
    missing: list[pd.Timestamp] = []
    for date in unique_dates:
        active = hist.loc[
            (hist["in_date"] <= date) & (hist["out_date"] >= date), "symbol"
        ]
        if active.empty:
            missing.append(date)
        else:
            active_by_date[date] = set(active.tolist())
    if missing and require_complete:
        raise RuntimeError(
            f"{index_code} PIT 成分股历史未覆盖 {len(missing)} 个交易日，"
            f"范围 {missing[0].date()}~{missing[-1].date()}；正式回测被阻断。"
        )
    keep = pd.Series(
        [str(sym).zfill(6) in active_by_date.get(date, set())
         for sym, date in zip(bars["symbol"], bars_dates, strict=True)],
        index=bars.index,
    )
    return bars.loc[keep].copy()


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
