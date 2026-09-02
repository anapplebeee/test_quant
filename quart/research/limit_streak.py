"""Point-in-time daily event study for A-share consecutive limit-up stocks.

The module studies a close-confirmed signal only. A streak observed at the T close
can first be traded at the T+1 open. An upper-limit T+1 open is treated as
unbuyable; a lower-limit intended exit is delayed until the first executable open.
It is an event-study primitive, not an intraday queue simulator.
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
from scipy import stats

from quart.data.market import MarketData
from quart.execution.constraints import LIMIT_TOLERANCE
from quart.research.event_factors import price_limit_panel


def consecutive_true_counts(mask: pd.DataFrame) -> pd.DataFrame:
    """Return consecutive True counts per column, resetting to zero on False."""
    values = mask.fillna(False).to_numpy(dtype=bool, copy=False)
    output = np.zeros(values.shape, dtype=np.int16)
    running = np.zeros(values.shape[1], dtype=np.int16)
    for row in range(values.shape[0]):
        running = np.where(values[row], running + 1, 0)
        output[row] = running
    return pd.DataFrame(output, index=mask.index, columns=mask.columns)


def close_limit_hits(
    market: MarketData,
    *,
    limits: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return close-at-upper-limit flags and the aligned historical limit panel."""
    close = market.closes.astype("float64")
    previous = market.close_val.shift(1).astype("float64")
    limit_panel = (
        price_limit_panel(market.dates, market.symbols)
        if limits is None
        else limits.reindex(index=market.dates, columns=market.symbols)
    ).astype("float64")
    tradable = market.volumes.fillna(0).gt(0) & close.notna() & previous.notna()
    theoretical_up = (previous * (1.0 + limit_panel)).round(2)
    hit = close.ge(theoretical_up - LIMIT_TOLERANCE) & tradable & limit_panel.notna()
    return hit, limit_panel


def _normalise_ints(values: Iterable[int], *, name: str) -> tuple[int, ...]:
    result = tuple(sorted({int(value) for value in values}))
    if not result or result[0] < 1:
        raise ValueError(f"{name} must contain positive integers")
    return result


def build_limit_streak_events(
    market: MarketData,
    *,
    benchmark_open: pd.Series | None = None,
    levels: Iterable[int] = (1, 2, 3, 4, 5),
    horizons: Iterable[int] = (1, 2, 3, 5),
    min_avg_amount: float = 50_000_000.0,
    adv_window: int = 20,
    max_exit_delay: int = 5,
    limits: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build executable event rows for first reaching each requested streak level.

    A row with ``streak_level=4`` is the first four-board signal, not every later
    day of the same chain. Returns use T+1 open entry and T+1+h open exit. When the
    intended exit opens at the lower limit or is suspended, exit is delayed up to
    ``max_exit_delay`` trading days.
    """
    level_values = _normalise_ints(levels, name="levels")
    horizon_values = _normalise_ints(horizons, name="horizons")
    if adv_window < 1:
        raise ValueError("adv_window must be positive")
    if min_avg_amount < 0:
        raise ValueError("min_avg_amount must be non-negative")
    if max_exit_delay < 0:
        raise ValueError("max_exit_delay must be non-negative")

    hit, limit_panel = close_limit_hits(market, limits=limits)
    streak = consecutive_true_counts(hit)
    requested = streak.isin(level_values)

    if market.amounts is None:
        adv = pd.DataFrame(np.nan, index=market.dates, columns=market.symbols)
        liquid = pd.DataFrame(False, index=market.dates, columns=market.symbols)
    else:
        adv = market.amounts.astype("float64").rolling(
            adv_window, min_periods=max(1, min(10, adv_window))
        ).mean()
        liquid = adv.ge(float(min_avg_amount))
    signal_mask = requested & liquid & market.volumes.fillna(0).gt(0)

    dates = pd.DatetimeIndex(market.dates)
    symbols = np.asarray(market.symbols.astype(str))
    opens = market.opens.to_numpy(dtype="float64", copy=False)
    highs = market.highs.to_numpy(dtype="float64", copy=False)
    lows = market.lows.to_numpy(dtype="float64", copy=False)
    closes = market.closes.to_numpy(dtype="float64", copy=False)
    close_values = market.close_val.to_numpy(dtype="float64", copy=False)
    volumes = market.volumes.fillna(0).to_numpy(dtype="float64", copy=False)
    limits_array = limit_panel.to_numpy(dtype="float64", copy=False)
    streak_array = streak.to_numpy(dtype=np.int16, copy=False)
    adv_array = adv.to_numpy(dtype="float64", copy=False)

    eligible_forward: dict[int, pd.Series] = {}
    benchmark_forward: dict[int, pd.Series] = {}
    for horizon in horizon_values:
        forward = market.opens.shift(-(1 + horizon)).div(
            market.opens.shift(-1).replace(0, np.nan)
        ) - 1.0
        eligible_forward[horizon] = forward.where(liquid).mean(axis=1)
        if benchmark_open is not None:
            bench = pd.to_numeric(benchmark_open, errors="coerce").reindex(dates)
            benchmark_forward[horizon] = bench.shift(-(1 + horizon)).div(
                bench.shift(-1).replace(0, np.nan)
            ) - 1.0

    rows: list[dict[str, object]] = []
    positions = np.argwhere(signal_mask.to_numpy(dtype=bool, copy=False))
    max_needed = 1 + max(horizon_values)
    for signal_index, symbol_index in positions:
        if signal_index + max_needed >= len(dates):
            continue
        signal_close = closes[signal_index, symbol_index]
        entry_index = signal_index + 1
        entry_open = opens[entry_index, symbol_index]
        entry_limit = limits_array[entry_index, symbol_index]
        entry_volume = volumes[entry_index, symbol_index]
        entry_reason: str | None = None
        entry_upper = np.nan
        if not np.isfinite(entry_open) or entry_volume <= 0:
            entry_reason = "suspended_or_missing"
        elif not np.isfinite(signal_close) or not np.isfinite(entry_limit):
            entry_reason = "missing_limit_rule"
        else:
            entry_upper = round(signal_close * (1.0 + entry_limit), 2)
            if entry_open >= entry_upper - LIMIT_TOLERANCE:
                entry_reason = "open_at_upper_limit"
        promoted_next_day = bool(
            streak_array[entry_index, symbol_index]
            == streak_array[signal_index, symbol_index] + 1
        )
        next_day_one_word = bool(
            promoted_next_day
            and np.isfinite(entry_upper)
            and all(
                np.isfinite(value) and abs(value - entry_upper) <= LIMIT_TOLERANCE
                for value in (
                    opens[entry_index, symbol_index],
                    highs[entry_index, symbol_index],
                    lows[entry_index, symbol_index],
                    closes[entry_index, symbol_index],
                )
            )
        )

        for horizon in horizon_values:
            target_exit_index = entry_index + horizon
            base = {
                "signal_date": dates[signal_index],
                "symbol": symbols[symbol_index],
                "streak_level": int(streak_array[signal_index, symbol_index]),
                "horizon": horizon,
                "signal_close": signal_close,
                "entry_date": dates[entry_index],
                "entry_open": entry_open,
                "entry_upper_limit": entry_upper,
                "entry_block_reason": entry_reason,
                "promoted_next_day": promoted_next_day,
                "next_day_one_word": next_day_one_word,
                "adv": adv_array[signal_index, symbol_index],
                "capacity_at_1pct_adv": adv_array[signal_index, symbol_index] * 0.01,
                "intended_exit_date": dates[target_exit_index],
                "eligible_universe_return": eligible_forward[horizon].iloc[signal_index],
                "benchmark_return": (
                    benchmark_forward[horizon].iloc[signal_index]
                    if horizon in benchmark_forward
                    else np.nan
                ),
            }
            if entry_reason is not None:
                rows.append({
                    **base,
                    "actual_exit_date": pd.NaT,
                    "actual_exit_open": np.nan,
                    "exit_delay": np.nan,
                    "exit_unresolved": True,
                    "gross_return": np.nan,
                })
                continue

            actual_exit_index: int | None = None
            last_exit_index = min(target_exit_index + max_exit_delay, len(dates) - 1)
            for candidate_index in range(target_exit_index, last_exit_index + 1):
                candidate_open = opens[candidate_index, symbol_index]
                candidate_volume = volumes[candidate_index, symbol_index]
                candidate_limit = limits_array[candidate_index, symbol_index]
                previous_close = close_values[candidate_index - 1, symbol_index]
                if (
                    not np.isfinite(candidate_open)
                    or candidate_volume <= 0
                    or not np.isfinite(candidate_limit)
                    or not np.isfinite(previous_close)
                ):
                    continue
                lower = round(previous_close * (1.0 - candidate_limit), 2)
                if candidate_open <= lower + LIMIT_TOLERANCE:
                    continue
                actual_exit_index = candidate_index
                break

            if actual_exit_index is None:
                rows.append({
                    **base,
                    "actual_exit_date": pd.NaT,
                    "actual_exit_open": np.nan,
                    "exit_delay": np.nan,
                    "exit_unresolved": True,
                    "gross_return": np.nan,
                })
                continue
            actual_exit = opens[actual_exit_index, symbol_index]
            rows.append({
                **base,
                "actual_exit_date": dates[actual_exit_index],
                "actual_exit_open": actual_exit,
                "exit_delay": int(actual_exit_index - target_exit_index),
                "exit_unresolved": False,
                "gross_return": float(actual_exit / entry_open - 1.0),
            })

    return pd.DataFrame(rows)


def _fdr_bh(pvalues: pd.Series) -> pd.Series:
    """Benjamini-Hochberg q-values, preserving missing values and index."""
    valid = pvalues.dropna().astype(float)
    result = pd.Series(np.nan, index=pvalues.index, dtype=float)
    if valid.empty:
        return result
    ordered = valid.sort_values()
    count = len(ordered)
    adjusted = ordered * count / np.arange(1, count + 1)
    adjusted = adjusted.iloc[::-1].cummin().iloc[::-1].clip(upper=1.0)
    result.loc[adjusted.index] = adjusted
    return result


def summarize_limit_streak_events(
    events: pd.DataFrame,
    *,
    cost_bps: float,
    split_date: str | pd.Timestamp,
    max_positions: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Summarize daily equal-weight event baskets, baselines, stability, and FDR."""
    if cost_bps < 0:
        raise ValueError("cost_bps must be non-negative")
    if max_positions < 1:
        raise ValueError("max_positions must be positive")
    if events.empty:
        return pd.DataFrame(), pd.DataFrame()
    split = pd.Timestamp(split_date)
    summary_rows: list[dict[str, object]] = []
    period_rows: list[dict[str, object]] = []

    for (level, horizon), group in events.groupby(["streak_level", "horizon"], sort=True):
        # Selection uses only T-known ADV. Blocked entries remain cash; do not replace
        # them with lower-ranked names after observing the T+1 open.
        selected = (
            group.sort_values(["signal_date", "adv"], ascending=[True, False])
            .groupby("signal_date", sort=False, group_keys=False)
            .head(max_positions)
            .copy()
        )
        buyable = selected[selected["entry_block_reason"].isna()]
        resolved = buyable[buyable["gross_return"].notna()].copy()
        # Every selected name owns one target slot. A rejected entry leaves that
        # slot in cash instead of redistributing it after seeing T+1 execution.
        selected["strategy_return"] = selected["gross_return"].fillna(0.0)
        daily = selected.groupby("signal_date", sort=True).agg(
            gross_return=("strategy_return", "mean"),
            eligible_universe_return=("eligible_universe_return", "first"),
            benchmark_return=("benchmark_return", "first"),
        )
        daily["excess_eligible"] = daily["gross_return"] - daily["eligible_universe_return"]
        daily["excess_benchmark"] = daily["gross_return"] - daily["benchmark_return"]
        daily["net_1x"] = daily["gross_return"] - cost_bps / 10_000.0
        daily["net_2x"] = daily["gross_return"] - 2 * cost_bps / 10_000.0
        daily["net_3x"] = daily["gross_return"] - 3 * cost_bps / 10_000.0
        excess = daily["excess_eligible"].dropna()
        pvalue = (
            float(stats.ttest_1samp(excess, 0.0, nan_policy="omit").pvalue)
            if len(excess) >= 3 and excess.std(ddof=1) > 0
            else np.nan
        )

        row: dict[str, object] = {
            "streak_level": int(level),
            "horizon": int(horizon),
            "signals": len(group),
            "signal_days": int(group["signal_date"].nunique()),
            "selected_signals": len(selected),
            "buyable": len(buyable),
            "entry_block_rate": (
                float(1.0 - len(buyable) / len(selected)) if len(selected) else np.nan
            ),
            "resolved": len(resolved),
            "exit_unresolved_rate": (
                float(buyable["exit_unresolved"].mean()) if len(buyable) else np.nan
            ),
            "exit_delay_rate": float(resolved["exit_delay"].gt(0).mean()) if len(resolved) else np.nan,
            "mean_event_return": float(resolved["gross_return"].mean()) if len(resolved) else np.nan,
            "median_event_return": float(resolved["gross_return"].median()) if len(resolved) else np.nan,
            "daily_basket_return": float(daily["gross_return"].mean()) if len(daily) else np.nan,
            "daily_win_rate": float(daily["gross_return"].gt(0).mean()) if len(daily) else np.nan,
            "eligible_baseline_return": float(daily["eligible_universe_return"].mean()) if len(daily) else np.nan,
            "benchmark_return": float(daily["benchmark_return"].mean()) if len(daily) else np.nan,
            "excess_eligible": float(daily["excess_eligible"].mean()) if len(daily) else np.nan,
            "excess_benchmark": float(daily["excess_benchmark"].mean()) if len(daily) else np.nan,
            "net_1x": float(daily["net_1x"].mean()) if len(daily) else np.nan,
            "net_2x": float(daily["net_2x"].mean()) if len(daily) else np.nan,
            "net_3x": float(daily["net_3x"].mean()) if len(daily) else np.nan,
            "excess_pvalue": pvalue,
            "median_adv": float(resolved["adv"].median()) if len(resolved) else np.nan,
            "p10_capacity_at_1pct_adv": (
                float(resolved["capacity_at_1pct_adv"].quantile(0.10)) if len(resolved) else np.nan
            ),
        }
        for label, selector in (
            ("early", daily.index < split),
            ("late", daily.index >= split),
        ):
            period = daily.loc[selector]
            row[f"{label}_net_1x"] = float(period["net_1x"].mean()) if len(period) else np.nan
            row[f"{label}_excess_eligible"] = (
                float(period["excess_eligible"].mean()) if len(period) else np.nan
            )
            period_rows.append({
                "period": label,
                "streak_level": int(level),
                "horizon": int(horizon),
                "signal_days": len(period),
                "daily_basket_return": float(period["gross_return"].mean()) if len(period) else np.nan,
                "net_1x": float(period["net_1x"].mean()) if len(period) else np.nan,
                "excess_eligible": float(period["excess_eligible"].mean()) if len(period) else np.nan,
                "excess_benchmark": float(period["excess_benchmark"].mean()) if len(period) else np.nan,
            })
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    # First-board rows are the comparison baseline, not part of the 16 candidate
    # family: heights 2/3/4/5 x horizons 1/2/3/5.
    summary["fdr_qvalue"] = np.nan
    candidate_family = summary["streak_level"].ge(2)
    summary.loc[candidate_family, "fdr_qvalue"] = _fdr_bh(
        summary.loc[candidate_family, "excess_pvalue"]
    )
    enough = summary["signal_days"].ge(50)
    stable = summary["early_excess_eligible"].gt(0) & summary["late_excess_eligible"].gt(0)
    summary["candidate_gate"] = (
        summary["streak_level"].ge(2)
        & enough
        & stable
        & summary["net_1x"].gt(0)
        & summary["excess_eligible"].gt(0)
        & summary["excess_benchmark"].gt(0)
        & summary["fdr_qvalue"].le(0.10)
        & summary["exit_unresolved_rate"].le(0.01)
    )
    return summary, pd.DataFrame(period_rows)


def summarize_limit_streak_progression(
    events: pd.DataFrame,
    *,
    max_positions: int = 10,
) -> pd.DataFrame:
    """Summarize next-day board promotion and queue-risk diagnostics by height."""
    if max_positions < 1:
        raise ValueError("max_positions must be positive")
    if events.empty:
        return pd.DataFrame()
    base = (
        events.sort_values("horizon")
        .drop_duplicates(["signal_date", "symbol", "streak_level"])
        .copy()
    )
    rows: list[dict[str, object]] = []
    for level, group in base.groupby("streak_level", sort=True):
        selected = (
            group.sort_values(["signal_date", "adv"], ascending=[True, False])
            .groupby("signal_date", sort=False, group_keys=False)
            .head(max_positions)
        )
        selected_buyable = selected[selected["entry_block_reason"].isna()]
        selected_blocked = selected[
            selected["entry_block_reason"].eq("open_at_upper_limit")
        ]
        selected_promoted = selected[selected["promoted_next_day"]]
        rows.append({
            "streak_level": int(level),
            "events": len(group),
            "signal_days": int(group["signal_date"].nunique()),
            "promotion_rate": float(group["promoted_next_day"].mean()),
            "one_word_rate": float(group["next_day_one_word"].mean()),
            "selected_events": len(selected),
            "selected_promotion_rate": float(selected["promoted_next_day"].mean()),
            "selected_open_limit_block_rate": float(
                selected["entry_block_reason"].eq("open_at_upper_limit").mean()
            ),
            "selected_one_word_rate": float(selected["next_day_one_word"].mean()),
            "selected_buyable_promotion_rate": (
                float(selected_buyable["promoted_next_day"].mean())
                if len(selected_buyable)
                else np.nan
            ),
            "selected_blocked_promotion_rate": (
                float(selected_blocked["promoted_next_day"].mean())
                if len(selected_blocked)
                else np.nan
            ),
            "promotion_capture_rate": (
                float(selected_promoted["entry_block_reason"].isna().mean())
                if len(selected_promoted)
                else np.nan
            ),
        })
    return pd.DataFrame(rows)


__all__ = [
    "build_limit_streak_events",
    "close_limit_hits",
    "consecutive_true_counts",
    "summarize_limit_streak_events",
    "summarize_limit_streak_progression",
]
