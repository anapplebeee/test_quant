from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from quart.config import PROJECT_ROOT

INDUSTRY_PATH = PROJECT_ROOT / "data" / "universe" / "sw_industry.parquet"
STAT_INDUSTRY_PATH = PROJECT_ROOT / "data" / "universe" / "stat_industry.parquet"


def _load_sw(level: str) -> pd.Series | None:
    path = Path(INDUSTRY_PATH)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    col = {"first": "ind1", "second": "ind2", "third": "ind3"}[level]
    s = df.drop_duplicates("symbol").set_index("symbol")[col].fillna("UNKNOWN")
    s.index = s.index.astype(str)
    return s.astype(str)


@lru_cache
def load_industry_series(level: str = "first") -> pd.Series:
    """Shenwan map if available; otherwise fall back to statistical clusters."""
    from pathlib import Path

    sw = _load_sw(level)
    if sw is not None and len(sw) > 0:
        return sw.rename_axis("symbol")

    spath = Path(STAT_INDUSTRY_PATH)
    if not spath.exists():
        raise FileNotFoundError(
            f"no industry source: run scripts/fetch_industries.py (sw) or "
            f"scripts/build_stat_industries.py (statistical); missing {spath}"
        )
    df = pd.read_parquet(spath)
    series = df.set_index("symbol")["cluster"]
    series.index = series.index.astype(str)
    return series.astype(str)


def industry_neutralize(scores: pd.Series, industries: pd.Series, min_group_size: int = 5) -> pd.Series:
    """Subtract per-industry mean; groups smaller than min_group_size keep raw score."""
    joined = pd.DataFrame({"score": scores}).join(industries.rename("ind"), how="left")
    joined["ind"] = joined["ind"].fillna("UNKNOWN")
    grp = joined.groupby("ind")["score"]
    mean = grp.transform("mean")
    count = grp.transform("count")
    residual = joined["score"] - mean
    return residual.where(count >= min_group_size, joined["score"])
