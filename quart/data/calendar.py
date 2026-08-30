"""A 股交易日历缓存与查询。"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from quart.config import PROJECT_ROOT

DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "data" / "meta" / "trading_calendar.csv"


def normalize_trade_date(value: str | date | datetime | pd.Timestamp) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


class TradingCalendar:
    """只读交易日历；缓存缺失时明确退化为工作日规则。"""

    def __init__(self, dates: Iterable[str | date | datetime | pd.Timestamp] = ()):
        self.dates = tuple(sorted({normalize_trade_date(value) for value in dates}))

    @classmethod
    def from_csv(cls, path: Path | str = DEFAULT_CALENDAR_PATH) -> "TradingCalendar":
        source = Path(path)
        if not source.exists():
            return cls()
        frame = pd.read_csv(source)
        column = next((name for name in ("trade_date", "date") if name in frame.columns), None)
        if column is None:
            raise ValueError(f"交易日历缺少 trade_date/date 列: {source}")
        return cls(frame[column].dropna().astype(str))

    @property
    def has_cache(self) -> bool:
        return bool(self.dates)

    def is_trade_date(self, value: str | date | datetime | pd.Timestamp) -> bool:
        current = normalize_trade_date(value)
        return current in set(self.dates) if self.dates else current.weekday() < 5

    def next_after(self, value: str | date | datetime | pd.Timestamp) -> date:
        current = normalize_trade_date(value)
        future = next((candidate for candidate in self.dates if candidate > current), None)
        if future is not None:
            return future
        candidate = current + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


def cached_trading_dates(path: Path | str = DEFAULT_CALENDAR_PATH) -> tuple[str, ...]:
    return tuple(value.isoformat() for value in TradingCalendar.from_csv(path).dates)


def next_market_trade_date(
    value: str | date | datetime | pd.Timestamp,
    path: Path | str = DEFAULT_CALENDAR_PATH,
) -> str:
    return TradingCalendar.from_csv(path).next_after(value).isoformat()


__all__ = [
    "DEFAULT_CALENDAR_PATH",
    "TradingCalendar",
    "cached_trading_dates",
    "next_market_trade_date",
    "normalize_trade_date",
]
