from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from quart.config import PROJECT_ROOT

INDUSTRY_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry.parquet"


@lru_cache
def load_industry_series(level: str = "first") -> pd.Series:
    """Return Series indexed by symbol with industry label at given level."""
    path = Path(INDUSTRY_PATH)
    if not path.exists():
        raise FileNotFoundError(f"industry map missing: {path}, run scripts/fetch_industries.py first")
    df = pd.read_parquet(path, dtype={"symbol": str})
    col = {"first": "ind1", "second": "ind2", "third": "ind3"}[level]
    series = df.drop_duplicates("symbol").set_index("symbol")[col].fillna("UNKNOWN")
    series.index = series.index.astype(str)
    return series


def industry_neutralize(scores: pd.Series, industries: pd.Series, min_group_size: int = 5) -> pd.Series:
    """Subtract per-industry mean; groups smaller than min_group_size keep raw score."""
    joined = pd.DataFrame({"score": scores}).join(industries.rename("ind"), how="left")
    joined["ind"] = joined["ind"].fillna("UNKNOWN")
    grp = joined.groupby("ind")["score"]
    mean = grp.transform("mean")
    count = grp.transform("count")
    residual = joined["score"] - mean
    return residual.where(count >= min_group_size, joined["score"])
