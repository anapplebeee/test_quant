"""共享测试夹具。

此前没有 conftest.py，每个测试文件各造各的合成行情面板，
导致"无未来函数""T+1"这类关键断言在 5 份稍有不同的数据上各测一遍。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.market import MarketData
from quart.execution.fees import Fees


def make_bars(specs: dict[str, float], dates, step: float = 1.0) -> pd.DataFrame:
    """合成日线：open=close=base+ramp，高低各外扩 0.5。"""
    frames = []
    for symbol, base_price in specs.items():
        ramp = np.arange(len(dates)) * step
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(dates),
            "symbol": symbol,
            "open": base_price + ramp,
            "high": base_price + ramp + 0.5,
            "low": base_price + ramp - 0.5,
            "close": base_price + ramp,
            "volume": 1_000_000.0,
            "amount": (base_price + ramp) * 1_000_000.0,
        }))
    return pd.concat(frames, ignore_index=True)


def flat_bars(specs: dict[str, float], dates) -> pd.DataFrame:
    """价格恒定的面板（隔离滑点/波动影响时用）。"""
    frames = []
    for symbol, price in specs.items():
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(dates),
            "symbol": symbol,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000_000.0,
            "amount": price * 1_000_000.0,
        }))
    return pd.concat(frames, ignore_index=True)


@pytest.fixture
def zero_fees() -> Fees:
    """零成本口径：用于隔离费用/滑点，只验证撮合与时序逻辑。"""
    return Fees(
        commission_rate=0.0, commission_min=0.0, stamp_tax_rate=0.0,
        transfer_fee_rate=0.0, slippage_rate=0.0, impact_coef=0.0,
    )


@pytest.fixture
def real_fees() -> Fees:
    """与 settings.yaml 一致的实盘级成本。"""
    return Fees.from_config()


@pytest.fixture
def md() -> MarketData:
    """三只股票、10 个交易日的上升面板。"""
    dates = pd.date_range("2024-01-01", periods=10)
    return MarketData.from_bars(make_bars({"A": 10.0, "B": 20.0, "C": 5.0}, dates, step=0.5))


@pytest.fixture
def bench() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=10)
    return pd.DataFrame({
        "date": dates, "symbol": ["IDX000300"] * len(dates),
        "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3000.0,
        "volume": 1e8, "amount": 3e11,
    })
