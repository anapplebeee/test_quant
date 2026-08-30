from quart.data.calendar import TradingCalendar, cached_trading_dates, next_market_trade_date
from quart.manual_trading.repository import next_trade_date


def test_calendar_skips_weekend_and_holiday(tmp_path):
    path = tmp_path / "calendar.csv"
    path.write_text(
        "trade_date\n2026-09-30\n2026-10-09\n2026-10-12\n",
        encoding="utf-8",
    )
    calendar = TradingCalendar.from_csv(path)
    assert calendar.next_after("2026-09-30").isoformat() == "2026-10-09"
    assert next_market_trade_date("2026-10-09", path) == "2026-10-12"


def test_calendar_is_trade_date_and_fallback():
    calendar = TradingCalendar(["2026-10-09", "2026-10-12"])
    assert calendar.is_trade_date("2026-10-09")
    # 2026-10-01 国庆节在缓存内 → 非交易日
    assert not calendar.is_trade_date("2026-10-01")
    assert not calendar.is_trade_date("2026-10-10")  # 周五但不在缓存
    assert not calendar.is_trade_date("2026-10-11")  # 周六

    empty = TradingCalendar()
    assert not empty.has_cache
    # 缓存缺失退化为工作日规则
    assert empty.is_trade_date("2026-10-01")  # 周四，无法识别节假日
    assert not empty.is_trade_date("2026-10-10")  # 周六


def test_next_trade_date_uses_cached_calendar_for_holidays(monkeypatch):
    """repository.settle 推进应使用日历缓存：跨国庆节假日而非顺延到 10-08。"""
    import quart.data.calendar as calendar_module

    monkeypatch.setattr(
        calendar_module,
        "cached_trading_dates",
        lambda: ("2026-09-30", "2026-10-09", "2026-10-12", "2026-10-13"),
    )
    # 09-30(周三) 买入 → settle 应为 10-09，而不是周末规则给出的 10-08
    assert next_trade_date("2026-09-30") == "2026-10-09"
    assert next_trade_date("2026-10-09") == "2026-10-12"


def test_manual_settlement_accepts_explicit_market_calendar():
    assert next_trade_date(
        "2026-09-30",
        ["2026-09-30", "2026-10-09", "2026-10-12"],
    ) == "2026-10-09"


def test_cached_trading_dates_missing_file_returns_empty(tmp_path):
    assert cached_trading_dates(tmp_path / "missing.csv") == ()

