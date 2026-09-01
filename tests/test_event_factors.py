from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.research.event_factors import (
    dragon_tiger_panels,
    event_sentiment_panels,
    limit_event_panels,
    neutralize_against,
    price_limit_panel,
)


def _market(dates: pd.DatetimeIndex, symbols: list[str]) -> MarketData:
    close = pd.DataFrame(10.0, index=dates, columns=symbols)
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=symbols)
    amount = volume * close * 100.0
    return MarketData(open_, close * 1.01, close * 0.99, close, volume, amounts=amount)


def test_price_limit_panel_uses_historical_chinext_rule():
    dates = pd.DatetimeIndex(["2020-08-21", "2020-08-24"])
    panel = price_limit_panel(dates, ["300001", "600000"])

    assert panel.loc[dates[0], "300001"] == np.float32(0.10)
    assert panel.loc[dates[1], "300001"] == np.float32(0.20)
    assert panel.loc[dates[1], "600000"] == np.float32(0.10)


def test_limit_event_panels_only_use_current_and_past_data():
    dates = pd.bdate_range("2024-01-02", periods=50)
    market = _market(dates, ["600000", "600001"])
    market.close_val.loc[dates[15], "600000"] = 11.0
    market.closes.loc[dates[15], "600000"] = 11.0
    original = limit_event_panels(market)

    changed = _market(dates, ["600000", "600001"])
    changed.close_val.loc[dates[15], "600000"] = 11.0
    changed.closes.loc[dates[15], "600000"] = 11.0
    changed.close_val.loc[dates[31]:, :] *= 3.0
    changed.closes.loc[dates[31]:, :] *= 3.0
    mutated = limit_event_panels(changed)

    assert original["limit_hit_count20_neg"].loc[dates[25], "600000"] == -1.0
    for name in original:
        pd.testing.assert_series_equal(
            original[name].loc[dates[30]], mutated[name].loc[dates[30]], check_names=False
        )


def test_neutralize_against_removes_cross_sectional_linear_exposure():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2025-01-02", periods=4)
    symbols = [f"{600000 + i:06d}" for i in range(30)]
    control = pd.DataFrame(rng.normal(size=(4, 30)), index=dates, columns=symbols)
    noise = pd.DataFrame(rng.normal(scale=0.1, size=(4, 30)), index=dates, columns=symbols)
    residual = neutralize_against(3.0 * control + noise, control)

    for date in dates:
        assert abs(residual.loc[date].corr(control.loc[date])) < 1e-7


def test_event_availability_respects_close_and_date_only_records():
    dates = pd.bdate_range("2025-01-02", periods=4)
    symbols = ["600000", "600001", "600002"]
    events = pd.DataFrame(
        {
            "symbol": symbols,
            "published_at": [
                "2025-01-02 14:00:00",
                "2025-01-02 16:00:00",
                "2025-01-02",
            ],
            "sentiment": [1.0, -1.0, 0.5],
        }
    )
    panel = event_sentiment_panels(events, dates, symbols)["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 1.0
    assert panel.loc[dates[0], "600001"] == 0.0
    assert panel.loc[dates[0], "600002"] == 0.0
    assert panel.loc[dates[1], "600001"] < 0.0
    assert panel.loc[dates[1], "600002"] > 0.0


def test_explicit_available_at_takes_precedence():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 10:00:00"],
            "available_at": ["2025-01-03 09:00:00"],
            "sentiment": [1.0],
        }
    )
    panel = event_sentiment_panels(events, dates, ["600000"])["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 0.0
    assert panel.loc[dates[1], "600000"] == 1.0


def test_available_at_cannot_precede_publication():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 16:00:00"],
            "available_at": ["2025-01-02 10:00:00"],
            "sentiment": [1.0],
        }
    )
    panel = event_sentiment_panels(events, dates, ["600000"])["event_sentiment_decay"]

    assert panel.loc[dates[0], "600000"] == 0.0
    assert panel.loc[dates[1], "600000"] == 1.0


def test_dragon_tiger_factor_is_normalized_by_disclosed_turnover():
    dates = pd.bdate_range("2025-01-02", periods=4)
    events = pd.DataFrame(
        {
            "symbol": ["600000"],
            "published_at": ["2025-01-02 16:00:00"],
            "net_buy_amount": [20.0],
            "institution_net_buy_amount": [5.0],
            "turnover_amount": [100.0],
        }
    )
    panels = dragon_tiger_panels(events, dates, ["600000"])

    assert panels["dragon_tiger_net_buy_decay"].loc[dates[1], "600000"] == np.float32(0.2)
    assert panels["dragon_tiger_institution_decay"].loc[dates[1], "600000"] == np.float32(0.05)
