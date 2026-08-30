"""数据源单位归一化测试（2026-08-31 审查新增）。

背景：`stock_zh_a_hist_tx` 对 000 开头返回 volume=手，其余返回 volume=股，
横截面差 100 倍，volume 类因子失真。`_normalize_volume_unit` 按量价关系自动统一为手。
"""
from __future__ import annotations

import pandas as pd

from quart.data.source_akshare import _normalize_volume_unit


def _frame(volume: float, close: float, amount: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-08-28"],
        "symbol": ["600519"],
        "open": [close], "high": [close], "low": [close], "close": [close],
        "volume": [volume], "amount": [amount],
    })


def test_volume_unit_shares_converted_to_lots():
    # 沪市风格：volume 单位是股（amount = close × volume），应 ÷100 转手
    df = _frame(volume=1_000_000, close=10.0, amount=10_000_000.0)
    out = _normalize_volume_unit(df)
    assert out["volume"].iloc[0] == 10_000  # 1,000,000 股 → 10,000 手


def test_volume_unit_lots_left_unchanged():
    # 深主板风格：volume 已是手（amount = close × volume × 100），保持不变
    df = _frame(volume=1_000_000, close=10.0, amount=1_000_000_000.0)
    out = _normalize_volume_unit(df)
    assert out["volume"].iloc[0] == 1_000_000


def test_volume_normalization_uses_median_ratio():
    # 混合多日：比值中位数判定应稳定
    rows = []
    for i in range(10):
        rows.append([f"2026-08-{10+i:02d}", "000001", 10.0, 10.0, 10.0, 10.0,
                     1_000_000.0, 1_000_000_000.0])
    df = pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close",
                                     "volume", "amount"])
    out = _normalize_volume_unit(df)
    assert out["volume"].iloc[0] == 1_000_000.0  # 已是手，不动


def test_volume_normalization_empty_safe():
    empty = pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume", "amount"])
    out = _normalize_volume_unit(empty)
    assert out.empty
    assert _normalize_volume_unit(None) is None
