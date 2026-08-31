"""PIT 基本面因子数据加载（换手率/流通市值/估值，baostock 回填口径）。

数据由 scripts/backfill_factor_data.py 生成，存放于
data/factors/fundamental_daily.parquet（长表：date, symbol, turn, float_mcap,
pe_ttm, pb, is_st）。所有字段均为时点真实值，不受复权基准漂移影响。
"""

from __future__ import annotations

from functools import lru_cache

import pandas as pd

from quart.config import data_root

REQUIRED_COLUMNS = ("date", "symbol", "turn", "float_mcap", "pe_ttm", "pb")


def fundamental_path():
    return data_root() / "factors" / "fundamental_daily.parquet"


@lru_cache(maxsize=1)
def load_fundamental() -> pd.DataFrame:
    """长表加载（带缓存）；文件不存在时抛出明确异常。"""
    path = fundamental_path()
    if not path.exists():
        raise FileNotFoundError(
            f"缺少基本面因子数据 {path}，请先运行 scripts/backfill_factor_data.py"
        )
    df = pd.read_parquet(path)
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"fundamental_daily.parquet 缺少列: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    return df


def fundamental_wide(column: str) -> pd.DataFrame:
    """透视成 date × symbol 宽表，供因子计算使用。"""
    df = load_fundamental()
    return df.pivot(index="date", columns="symbol", values=column)
