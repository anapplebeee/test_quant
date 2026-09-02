from __future__ import annotations

import pandas as pd
import pytest

from quart.execution.constraints import limit_prices
from quart.execution.fees import Fees
from quart.execution.minute_aware_model import MinuteAwareExecutionModel
from quart.execution.models import BUY, SELL, ExecutionContext

ZERO = Fees(0.0, 0.0, 0.0, 0.0, 0.0)


class _FakeStore:
    """按需返回构造的当日分钟 OHLC；data 为 {date: DataFrame} 或缺省。"""

    def __init__(self, per_day=None, fallback=None):
        self.per_day = per_day or {}
        self.fallback = fallback  # None 表示"文件缺失/无数据"

    def load(self, code, level="5"):
        # 返回该 code 的全部构造分钟（跨日），由 _load_minute 按执行日过滤
        if self.per_day:
            return pd.concat(list(self.per_day.values()), ignore_index=True)
        if self.fallback is not None:
            return self.fallback
        return pd.DataFrame()


def _minute_bars(day, prices_low_high):
    """构造 3 根 5min bar：[(low, high, close), ...]"""
    ts = pd.date_range(f"{day} 09:35:00", periods=len(prices_low_high), freq="5min")
    return pd.DataFrame(
        {
            "ts": ts,
            "level": "5",
            "open": [l for l, h, c in prices_low_high],
            "high": [h for l, h, c in prices_low_high],
            "low": [l for l, h, c in prices_low_high],
            "close": [c for l, h, c in prices_low_high],
            "volume": [1000.0] * len(prices_low_high),
            "amount": [1.0] * len(prices_low_high),
        }
    )


def _ctx(day: str):
    return ExecutionContext(
        date=pd.Timestamp(day),
        targets={},
        equity=1e6,
        cash=1e6,
        positions={},
        mark_prices=pd.Series(dtype=float),
        exec_prices=pd.Series(dtype=float),
        prev_closes=pd.Series(dtype=float),
    )


def test_limit_up_opens_intraday_lets_buy_through():
    """开盘一字涨停、但盘中开板（有 < 涨停价的成交）→ 买单放行。"""
    day = "2026-08-25"
    prev_close = 10.0
    up, _ = limit_prices(prev_close, "000001")
    # 盘中第一根就开板：low 远低于涨停价
    bars = _minute_bars(day, [(10.1, 11.0, 10.5)])
    store = _FakeStore(per_day={pd.Timestamp(day): bars})
    model = MinuteAwareExecutionModel(fees=ZERO, minute_store=store)
    model.bind_context(_ctx(day))
    assert model.blocked_reason("000001", BUY, up, prev_close) is None


def test_limit_up_sealed_all_day_blocks():
    """全天一字封死（所有分钟 low >= 涨停价）→ 买单维持拒单。"""
    day = "2026-08-25"
    prev_close = 10.0
    up, _ = limit_prices(prev_close, "000001")
    bars = _minute_bars(day, [(up, up, up), (up, up, up)])
    store = _FakeStore(per_day={pd.Timestamp(day): bars})
    model = MinuteAwareExecutionModel(fees=ZERO, minute_store=store)
    model.bind_context(_ctx(day))
    assert model.blocked_reason("000001", BUY, up, prev_close) is not None


def test_limit_down_opened_intraday_lets_sell():
    """开盘一字跌停但盘中开板（有 > 跌停价的成交）→ 卖单放行。"""
    day = "2026-08-25"
    prev_close = 10.0
    _, down = limit_prices(prev_close, "000001")
    bars = _minute_bars(day, [(down, 9.5, 9.3)])
    store = _FakeStore(per_day={pd.Timestamp(day): bars})
    model = MinuteAwareExecutionModel(fees=ZERO, minute_store=store)
    model.bind_context(_ctx(day))
    assert model.blocked_reason("000001", SELL, down, prev_close) is None


def test_no_minute_data_falls_back_conservatively():
    """当日无分钟数据（缺失）→ 回退父类：开盘触涨停仍拒单（保守）。"""
    day = "2026-08-25"
    prev_close = 10.0
    up, _ = limit_prices(prev_close, "000001")
    store = _FakeStore(fallback=pd.DataFrame())  # 无数据
    model = MinuteAwareExecutionModel(fees=ZERO, minute_store=store)
    model.bind_context(_ctx(day))
    assert model.blocked_reason("000001", BUY, up, prev_close) is not None


def test_non_limit_open_not_blocked():
    """开盘未触涨停（base_price < 涨停价）→ 正常可成交，不做盘中复核。"""
    prev_close = 10.0
    up, _ = limit_prices(prev_close, "000001")
    store = _FakeStore()  # 无分钟数据也不影响：父类已放行
    model = MinuteAwareExecutionModel(fees=ZERO, minute_store=store)
    model.bind_context(_ctx("2026-08-25"))
    assert model.blocked_reason("000001", BUY, up - 0.1, prev_close) is None
