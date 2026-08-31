"""领域时间处理：事件时间必须带时区，A 股日期默认解释为上海时区。"""
from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def market_datetime(
    trade_date: str | date | datetime,
    trade_time: str | None = None,
) -> datetime:
    """把历史兼容层的交易日/时间转换为带时区的 UTC 业务时间。"""
    if isinstance(trade_date, datetime):
        if trade_date.tzinfo is not None and trade_date.utcoffset() is not None:
            return trade_date.astimezone(UTC)
        market_day = trade_date.date()
        parsed_time = trade_date.time()
    elif isinstance(trade_date, date):
        market_day = trade_date
        parsed_time = time.min
    else:
        market_day = date.fromisoformat(str(trade_date))
        parsed_time = time.min

    if trade_time:
        parsed_time = time.fromisoformat(trade_time)
    event_time = datetime.combine(market_day, parsed_time)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=SHANGHAI_TZ)
    return event_time.astimezone(UTC)


__all__ = ["SHANGHAI_TZ", "market_datetime", "require_aware", "utc_now"]
