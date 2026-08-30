"""A 股交易约束常量与工具（板块涨跌停、整手）。

抽取动机：涨跌停/整手此前硬编码在回测引擎中，实盘计划路径完全没有涨跌停
检查，可能生成"次日一字板无法成交"的委托。
"""
from __future__ import annotations

import pandas as pd

#: A 股最小交易单位（股）
A_SHARE_LOT = 100

#: 清仓哨兵键：target_weights 返回 {FLAT: 1.0} 表示次日开盘全部卖出
FLAT = "__FLAT__"

#: 涨跌停幅度：代码前缀 → 幅度
#:   主板 10%；创业板(300/301)、科创板(688/689) 20%；北交所(43/8x/92) 30%
_PRICE_LIMIT_PREFIXES: tuple[tuple[tuple[str, ...], float], ...] = (
    (("300", "301", "688", "689"), 0.20),
    (("43", "82", "83", "87", "88", "92"), 0.30),
)

#: 涨跌停判断容差（元）。价格按分四舍五入，需留半个最小变动单位余量
LIMIT_TOLERANCE = 0.001


def price_limit_pct(symbol: str) -> float:
    """按代码前缀返回涨跌停幅度。"""
    code = str(symbol).split(".")[0]
    for prefixes, pct in _PRICE_LIMIT_PREFIXES:
        if code.startswith(prefixes):
            return pct
    return 0.10


def limit_prices(prev_close: float, symbol: str) -> tuple[float, float] | None:
    """返回 (涨停价, 跌停价)；prev_close 无效时返回 None。"""
    if pd.isna(prev_close) or prev_close <= 0:
        return None
    pct = price_limit_pct(symbol)
    up = round(prev_close * (1 + pct) + 1e-9, 2)
    down = round(prev_close * (1 - pct) - 1e-9, 2)
    return up, down


def is_limit_up(price: float, prev_close: float, symbol: str) -> bool:
    """开盘即涨停（买单无法成交）。"""
    lim = limit_prices(prev_close, symbol)
    return lim is not None and price >= lim[0] - LIMIT_TOLERANCE


def is_limit_down(price: float, prev_close: float, symbol: str) -> bool:
    """开盘即跌停（卖单无法成交）。"""
    lim = limit_prices(prev_close, symbol)
    return lim is not None and price <= lim[1] + LIMIT_TOLERANCE


def round_lot(shares: float) -> int:
    """向下取整到整手。"""
    return int(max(0, shares) // A_SHARE_LOT) * A_SHARE_LOT


__all__ = [
    "A_SHARE_LOT",
    "FLAT",
    "LIMIT_TOLERANCE",
    "is_limit_down",
    "is_limit_up",
    "limit_prices",
    "price_limit_pct",
    "round_lot",
]
