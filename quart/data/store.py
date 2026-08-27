from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pandas as pd
from loguru import logger

from quart.config import data_root

BAR_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]


def drop_incomplete_today(df: pd.DataFrame) -> pd.DataFrame:
    """盘中调用时剔除当天未收盘的K线，只保留已完成的历史bar."""
    if df is None or df.empty:
        return df
    now = dt.datetime.now()
    market_open_minutes = 15 * 60 + 30
    if now.hour * 60 + now.minute >= market_open_minutes:
        return df
    today_midnight = pd.Timestamp(now.date())
    return df[df["date"] < today_midnight]

EMPTY_BARS = pd.DataFrame({c: pd.Series(dtype=t) for c, t in {
    "date": "datetime64[ns]", "symbol": "object", "open": "float64",
    "high": "float64", "low": "float64", "close": "float64",
    "volume": "float64", "amount": "float64",
}.items()})


class BarStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else data_root()
        self.daily_dir = self.root / "daily"
        self.index_dir = self.root / "index"
        self.universe_dir = self.root / "universe"
        for d in (self.daily_dir, self.index_dir, self.universe_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        subdir = self.index_dir if symbol.startswith("IDX") else self.daily_dir
        return subdir / f"{symbol}.parquet"

    def save(self, df: pd.DataFrame, replace: bool = False) -> int:
        if df is None or df.empty:
            return 0
        df = df[BAR_COLUMNS].copy()
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume", "amount"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        written = 0
        for symbol, group in df.groupby("symbol"):
            path = self._path(str(symbol))
            if path.exists() and not replace:
                existing = pd.read_parquet(path)
                group = pd.concat([existing, group], ignore_index=True)
                group = group.drop_duplicates(subset=["date", "symbol"], keep="last")
            group = group.sort_values("date").reset_index(drop=True)
            group.to_parquet(path, index=False)
            written += len(group)
        return written

    def load(
        self,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_index: bool = False,
    ) -> pd.DataFrame:
        if symbols is not None:
            return self._load_symbols(list(symbols), start, end, include_index)
        dirs = [self.daily_dir] + ([self.index_dir] if include_index else [])
        files: list[Path] = []
        for d in dirs:
            files.extend(d.glob("*.parquet"))
        if not files:
            return EMPTY_BARS.copy()
        quoted = "[" + ", ".join(f"'{f.as_posix()}'" for f in sorted(files)) + "]"
        conds = []
        if start:
            conds.append(f"date >= '{start}'")
        if end:
            conds.append(f"date <= '{end}'")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        query = f"SELECT * FROM read_parquet({quoted}) {where} ORDER BY date, symbol"
        return duckdb.sql(query).df()

    def _load_symbols(
        self,
        symbols: list[str],
        start: str | None,
        end: str | None,
        include_index: bool,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for sym in symbols:
            if sym.startswith("IDX") and not include_index:
                missing.append(sym)
                continue
            path = self._path(sym)
            if not path.exists():
                missing.append(sym)
                continue
            frames.append(pd.read_parquet(path))
        if missing:
            logger.warning("symbols not in store: {}", sorted(missing)[:20])
        if not frames:
            return EMPTY_BARS.copy()
        out = pd.concat(frames, ignore_index=True)
        if start:
            out = out[out["date"] >= pd.Timestamp(start)]
        if end:
            out = out[out["date"] <= pd.Timestamp(end)]
        return out.sort_values(["date", "symbol"]).reset_index(drop=True)

    def load_benchmark(self, code: str) -> pd.DataFrame:
        path = self.index_dir / f"IDX{code}.parquet"
        if not path.exists():
            return EMPTY_BARS.copy()
        return pd.read_parquet(path)

    def last_date(self, symbol: str) -> pd.Timestamp | None:
        path = self._path(symbol)
        if not path.exists():
            return None
        dates = pd.read_parquet(path, columns=["date"])["date"]
        return None if dates.empty else pd.Timestamp(dates.max())

    def first_date(self, symbol: str) -> pd.Timestamp | None:
        path = self._path(symbol)
        if not path.exists():
            return None
        dates = pd.read_parquet(path, columns=["date"])["date"]
        return None if dates.empty else pd.Timestamp(dates.min())

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.daily_dir.glob("*.parquet"))
