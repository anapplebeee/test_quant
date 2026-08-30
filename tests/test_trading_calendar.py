from quart.data.calendar import TradingCalendar, next_market_trade_date
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


def test_manual_settlement_accepts_explicit_market_calendar():
    assert next_trade_date(
        "2026-09-30",
        ["2026-09-30", "2026-10-09", "2026-10-12"],
    ) == "2026-10-09"
