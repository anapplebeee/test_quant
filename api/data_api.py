"""数据 API - 数据总览相关。

路径全部走 `common.data_dir()`（源自 settings.yaml 的 data.root），
不再硬编码 "data"——此前改配置会导致核心库照新路径写、API 层读空且静默返回空。
"""
from __future__ import annotations

import pandas as pd

from common import daily_dir, data_dir, degraded, index_dir, safe_path, universe_dir, valid_symbol


def _count_parquet(directory) -> int:
    if not directory.exists():
        return 0
    return sum(1 for f in directory.glob("*.parquet"))


def get_stock_stats() -> dict:
    """获取股票统计数据"""
    scores_path = data_dir() / "scores" / "preds.csv"

    stats = {
        "stock_count": 0,
        "universe_count": 0,
        "index_count": 0,
        "last_score_date": "N/A",
    }

    try:
        stats["stock_count"] = _count_parquet(daily_dir())
    except Exception as e:
        degraded("stock_count", e)

    try:
        stats["universe_count"] = _count_parquet(universe_dir())
    except Exception as e:
        degraded("universe_count", e)

    try:
        stats["index_count"] = _count_parquet(index_dir())
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
    if not valid_symbol(symbol):
        return None
    path = safe_path(daily_dir(), f"{symbol}.parquet")
    if path is None or not path.exists():
        return None
    df = pd.read_parquet(path)
    return df if "date" in df.columns else None


def get_sample_data() -> pd.DataFrame | None:
    """获取样本数据（平安银行）"""
    try:
        return _read_daily("000001")
    except Exception as e:
        degraded("get_sample_data", e)
        return None


def get_stock_list() -> list[str]:
    """获取所有股票代码列表"""
    try:
        return sorted(f.stem for f in daily_dir().glob("*.parquet"))
    except Exception as e:
        degraded("get_stock_list", e)
        return []


def get_stock_data(symbol: str) -> pd.DataFrame | None:
    """获取指定股票的日线数据"""
    try:
        return _read_daily(symbol)
    except Exception as e:
        degraded("get_stock_data", e)
        return None
