"""数据 API - 数据总览相关。

路径统一走 `quart.data.store.BarStore`（分区/旧布局自动识别），
不再直读 `daily_dir()/*.parquet`——存储迁移为 year=YYYY 分区布局后，
旧 per-symbol 直读会静默返回空（2026-08-31 架构检视修复）。
"""
from __future__ import annotations

import pandas as pd

from common import degraded, index_dir, universe_dir


def _count_parquet(directory) -> int:
    if not directory.exists():
        return 0
    return sum(1 for f in directory.glob("*.parquet"))


def _count_partitioned(directory) -> int:
    """兼容分区布局（year=YYYY/*.parquet）与旧平铺布局。"""
    if not directory.exists():
        return 0
    flat = directory.glob("*.parquet")
    partitioned = directory.glob("year=*/*.parquet")
    return sum(1 for _ in flat) + sum(1 for _ in partitioned)


def _bar_store():
    from quart.data.store import BarStore

    return BarStore()


def get_stock_stats() -> dict:
    """获取股票统计数据（BarStore 双布局兼容）"""
    scores_path = _scores_path()

    stats = {
        "stock_count": 0,
        "universe_count": 0,
        "index_count": 0,
        "last_score_date": "N/A",
    }

    try:
        stats["stock_count"] = len(_bar_store().symbols())
    except Exception as e:
        degraded("stock_count", e)

    try:
        stats["universe_count"] = _count_parquet(universe_dir())
    except Exception as e:
        degraded("universe_count", e)

    try:
        stats["index_count"] = _count_partitioned(index_dir())
    except Exception as e:
        degraded("index_count", e)

    try:
        if scores_path.exists():
            scores_df = pd.read_csv(scores_path, usecols=["datetime"])
            stats["last_score_date"] = str(scores_df["datetime"].max())[:10]
    except Exception as e:
        degraded("last_score_date", e)

    return stats


def get_universe(limit: int = 50) -> pd.DataFrame:
    """获取最新股票池"""
    try:
        files = sorted(universe_dir().glob("*.parquet"))
        if not files:
            return pd.DataFrame(columns=["symbol", "名称"])
        df = pd.read_parquet(files[-1])

        try:
            from common import load_stock_names

            stock_names = load_stock_names()
            df["名称"] = df["symbol"].map(stock_names).fillna("-")
        except Exception as e:
            degraded("universe_names", e)

        return df[["symbol", "名称"]].head(limit) if "名称" in df.columns else df.head(limit)
    except Exception as e:
        degraded("get_universe", e)

    return pd.DataFrame(columns=["symbol", "名称"])


def _read_daily(symbol: str) -> pd.DataFrame | None:
    """个股全史日线（分区/旧布局自动识别）。"""
    try:
        bars = _bar_store().load(symbols=[str(symbol).zfill(6)])
        return bars if not bars.empty else None
    except Exception as e:
        degraded("get_stock_data", e)
        return None


def get_sample_data() -> pd.DataFrame | None:
    """获取样本数据（平安银行）"""
    return _read_daily("000001")


def get_stock_list() -> list[str]:
    """获取所有股票代码列表（双布局）"""
    try:
        return _bar_store().symbols()
    except Exception as e:
        degraded("get_stock_list", e)
        return []


def get_stock_data(symbol: str) -> pd.DataFrame | None:
    """获取指定股票的日线数据"""
    return _read_daily(symbol)


def _scores_path():
    from common import data_dir

    return data_dir() / "scores" / "preds.csv"
