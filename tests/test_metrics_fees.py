from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import Fees
from quart.backtest.metrics import cagr, max_drawdown, sharpe_ratio, summarize


def test_fee_math():
    fees = Fees(commission_rate=0.00025, commission_min=5.0, stamp_tax_rate=0.0005, transfer_fee_rate=0.00001)
    buy_amount = 100_000.0
    assert abs(fees.buy_cost(buy_amount) - (25.0 + 1.0)) < 1e-9

    small_amount = 10_000.0
    assert abs(fees.buy_cost(small_amount) - (max(2.5, 5.0) + 0.1)) < 1e-9

    sell_cost = fees.sell_cost(buy_amount)
    assert abs(sell_cost - (25.0 + 50.0 + 1.0)) < 1e-9


def test_slippage_direction():
    fees = Fees(slippage_rate=0.001)
    assert fees.buy_price(100.0) > 100.0
    assert fees.sell_price(100.0) < 100.0


def test_fee_scale_applies_to_all_cost_components():
    fees = Fees(
        commission_rate=0.00025,
        commission_min=5.0,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0.00001,
        slippage_rate=0.001,
        impact_coef=0.1,
    ).scaled(2)

    assert fees == Fees(
        commission_rate=0.0005,
        commission_min=10.0,
        stamp_tax_rate=0.001,
        transfer_fee_rate=0.00002,
        slippage_rate=0.002,
        impact_coef=0.2,
    )


def test_fee_scale_rejects_negative_multiplier():
    import pytest

    with pytest.raises(ValueError, match="non-negative"):
        Fees().scaled(-1)


def test_max_drawdown_known_series():
    eq = pd.Series([100.0, 110.0, 90.0, 100.0])
    mdd, trough = max_drawdown(eq)
    assert abs(mdd + (1 - 90.0 / 110.0)) < 1e-9
    assert trough == eq.index[2]


def test_sharpe_constant_series_is_zero_vol():
    eq = pd.Series([100.0] * 10)
    assert sharpe_ratio(eq) == 0.0


def test_drawdown_flat_market():
    eq = pd.Series(np.full(50, 12345.6))
    mdd, _ = max_drawdown(eq)
    assert mdd == 0.0


def test_excess_cagr_is_relative_nav_not_arithmetic_difference():
    """基准强势时，算术差会把负超额夸大近一个量级；
    修复后 excess_cagr 必须等于相对净值年化。"""
    days = 243 * 4
    idx = pd.bdate_range("2022-01-03", periods=days)
    strategy = pd.Series((1 + 0.00075) ** np.arange(days), index=idx)  # 年化 ~+20%
    benchmark = pd.Series((1 + 0.0037) ** np.arange(days), index=idx)  # 年化 ~+145%
    summary = summarize(strategy, benchmark=benchmark)

    relative = (strategy / strategy.iloc[0]) / (benchmark / benchmark.iloc[0])
    expected = cagr(relative)
    assert summary["bench_excess_cagr"] == pytest.approx(expected, abs=1e-9)
    # 与旧算术差口径显著不同（旧口径 -125% vs 相对口径 -51%）
    assert summary["bench_excess_cagr"] > summary["cagr"] - summary["bench_cagr"] + 0.1
    assert summary["bench_excess_cagr"] < 0.0


def test_excess_cagr_equals_benchmark_when_identical():
    days = 243 * 3
    idx = pd.bdate_range("2022-01-03", periods=days)
    identical = pd.Series((1 + 0.001) ** np.arange(days), index=idx)
    summary = summarize(identical, benchmark=identical.copy())
    assert summary["bench_excess_cagr"] == pytest.approx(0.0, abs=1e-12)
