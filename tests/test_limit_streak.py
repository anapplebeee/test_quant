from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.market import MarketData
from quart.research.limit_streak import (
    build_limit_streak_events,
    consecutive_true_counts,
    summarize_limit_streak_events,
    summarize_limit_streak_progression,
)


def _market(
    opens: list[float],
    closes: list[float],
    *,
    volumes: list[float] | None = None,
) -> tuple[MarketData, pd.DataFrame]:
    dates = pd.bdate_range("2025-01-02", periods=len(opens))
    columns = ["600000"]
    open_frame = pd.DataFrame({"600000": opens}, index=dates)
    close_frame = pd.DataFrame({"600000": closes}, index=dates)
    volume_frame = pd.DataFrame(
        {"600000": volumes or [1_000_000.0] * len(opens)}, index=dates
    )
    amount = volume_frame * open_frame
    market = MarketData(
        opens=open_frame,
        highs=pd.DataFrame(np.maximum(open_frame, close_frame), index=dates, columns=columns),
        lows=pd.DataFrame(np.minimum(open_frame, close_frame), index=dates, columns=columns),
        closes=close_frame,
        volumes=volume_frame,
        amounts=amount,
    )
    limits = pd.DataFrame(0.10, index=dates, columns=columns)
    return market, limits


def test_consecutive_true_counts_reset_per_symbol():
    mask = pd.DataFrame(
        {"a": [False, True, True, False, True], "b": [True, True, False, True, True]}
    )
    result = consecutive_true_counts(mask)
    assert result["a"].tolist() == [0, 1, 2, 0, 1]
    assert result["b"].tolist() == [1, 2, 0, 1, 2]


def test_future_hits_do_not_change_past_streaks():
    base = pd.DataFrame({"a": [True, True, False, False]})
    changed = base.copy()
    changed.loc[2:, "a"] = True
    left = consecutive_true_counts(base)
    right = consecutive_true_counts(changed)
    pd.testing.assert_series_equal(left.loc[:1, "a"], right.loc[:1, "a"])


def test_second_board_signal_enters_next_open_and_delays_limit_down_exit():
    market, limits = _market(
        opens=[10.0, 10.5, 11.4, 10.89, 9.80, 10.0, 10.1, 10.2],
        closes=[10.0, 11.0, 12.1, 10.89, 9.80, 10.0, 10.1, 10.2],
    )
    events = build_limit_streak_events(
        market,
        levels=(2,),
        horizons=(1,),
        min_avg_amount=0,
        adv_window=1,
        max_exit_delay=2,
        limits=limits,
    )
    assert len(events) == 1
    event = events.iloc[0]
    assert event["streak_level"] == 2
    assert event["entry_date"] == market.dates[3]
    assert pd.isna(event["entry_block_reason"])
    assert event["intended_exit_date"] == market.dates[4]
    assert event["actual_exit_date"] == market.dates[5]
    assert event["exit_delay"] == 1
    assert event["gross_return"] == pytest.approx(10.0 / 10.89 - 1.0)


def test_upper_limit_open_is_rejected():
    market, limits = _market(
        opens=[10.0, 10.5, 12.1, 12.2, 12.3, 12.4],
        closes=[10.0, 11.0, 12.1, 12.2, 12.3, 12.4],
    )
    events = build_limit_streak_events(
        market,
        levels=(1,),
        horizons=(1,),
        min_avg_amount=0,
        adv_window=1,
        limits=limits,
    )
    first = events.iloc[0]
    assert first["entry_block_reason"] == "open_at_upper_limit"
    assert pd.isna(first["gross_return"])
    progression = summarize_limit_streak_progression(events)
    assert progression.iloc[0]["promotion_rate"] == pytest.approx(1.0)
    assert progression.iloc[0]["one_word_rate"] == pytest.approx(1.0)
    assert progression.iloc[0]["promotion_capture_rate"] == pytest.approx(0.0)


def test_summary_requires_stable_cost_after_excess_for_candidate():
    dates = pd.bdate_range("2024-01-02", periods=60).append(
        pd.bdate_range("2025-01-02", periods=60)
    )
    events = pd.DataFrame(
        {
            "signal_date": dates,
            "symbol": ["600000"] * len(dates),
            "streak_level": [2] * len(dates),
            "horizon": [1] * len(dates),
            "entry_block_reason": [None] * len(dates),
            "exit_unresolved": [False] * len(dates),
            "gross_return": np.linspace(0.015, 0.025, len(dates)),
            "eligible_universe_return": [0.001] * len(dates),
            "benchmark_return": [0.001] * len(dates),
            "exit_delay": [0] * len(dates),
            "adv": [100_000_000.0] * len(dates),
            "capacity_at_1pct_adv": [1_000_000.0] * len(dates),
        }
    )
    summary, periods = summarize_limit_streak_events(
        events, cost_bps=30.0, split_date="2025-01-01"
    )
    assert bool(summary.iloc[0]["candidate_gate"]) is True
    assert set(periods["period"]) == {"early", "late"}


def test_summary_keeps_rejected_entry_weight_in_cash():
    events = pd.DataFrame(
        {
            "signal_date": [pd.Timestamp("2025-01-02")] * 2,
            "symbol": ["600000", "600001"],
            "streak_level": [2, 2],
            "horizon": [1, 1],
            "entry_block_reason": [None, "open_at_upper_limit"],
            "exit_unresolved": [False, True],
            "gross_return": [0.10, np.nan],
            "eligible_universe_return": [0.0, 0.0],
            "benchmark_return": [0.0, 0.0],
            "exit_delay": [0, np.nan],
            "adv": [100_000_000.0, 90_000_000.0],
            "capacity_at_1pct_adv": [1_000_000.0, 900_000.0],
        }
    )
    summary, _ = summarize_limit_streak_events(
        events, cost_bps=0.0, split_date="2025-01-01", max_positions=2
    )
    assert summary.iloc[0]["daily_basket_return"] == pytest.approx(0.05)
