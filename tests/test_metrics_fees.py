from __future__ import annotations

import numpy as np
import pandas as pd

from quart.backtest.engine import Fees
from quart.backtest.metrics import max_drawdown, sharpe_ratio


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
