"""ht_universe.py — 热点/龙头研究用的两层股票池（逐日 PIT、无前视）。

设计动机
--------
3 万本金追热点，A股最小 100 股/手 + T+1 + 手续费最低 5 元，决定了并非全市场
股票都"买得起/卖得掉"。本模块在完整后端 domain 代码之上，为每个交易日产出两层池：

1. training_pool（训练池，更宽）：
   剔除 ST/退市、科创/创业板、次新股(<list_days)、当日无量/停牌、退市后幽灵行情，
   保留所有主板活跃股（含高价龙头）——让模型学到完整模式。

2. position_pool（持仓池，严格 3 万可负担）：
   在 training_pool 之上再按流通市值区间/换手/日成交额/单手握金额过滤，确保
   3 万本金可成交、可持有多只。

关键约束与实现差异
------------------
- ST 判断用 fundamental_daily.is_st（逐日 PIT 标记），不用"当前名称含 ST"——
  否则会把"历史非 ST、当前被 ST"的样本误剔，或"历史 ST、当前摘帽"的样本漏剔。
- 板块(科创/创业)与次新(上市天数)用代码前缀 / list_dates 首日判断，均无前视。
- 只使用当日及之前可得信息；T 日收盘决策、T+1 下单。
- 复用（不重造）：quart.config / quart.data.universe / quart.data.delisted /
  quart.data.store / quart.data.fundamental。
"""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from quart.config import load_config
from quart.data.delisted import filter_delisted_bars
from quart.data.fundamental import load_fundamental
from quart.data.store import BarStore
from quart.data.universe import (
    STAR_PREFIXES,
    CHINEXT_PREFIXES,
    BSE_PREFIXES,
    get_list_dates,
)

MAINBOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


@lru_cache(maxsize=1)
def _fund() -> pd.DataFrame:
    return load_fundamental()


@lru_cache(maxsize=1)
def _list_days_cfg() -> int:
    try:
        return int(load_config()["data"].get("min_list_days", 120))
    except Exception:
        return 120


def _is_mainboard(codes: pd.Series) -> pd.Series:
    return codes.apply(lambda c: str(c).startswith(MAINBOARD_PREFIXES))


def training_pool(
    bars: pd.DataFrame,
    list_days: int | None = None,
    require_fundamental: bool = True,
) -> pd.DataFrame:
    """训练池：主板 + PIT非ST + 非退市 + 非次新 + 有量，保留高价龙头。返回逐日逐股长表。"""
    out = bars.copy()
    out["symbol"] = out["symbol"].astype(str).str.zfill(6)
    # 1) 板块：仅主板（排除科创/创业/北交所，规则统一为 10% 涨跌停）
    out = out[_is_mainboard(out["symbol"])]
    # 2) 当日无量/停牌不可成交
    out = out[out["amount"] > 0]
    # 3) 退市后幽灵行情
    out = filter_delisted_bars(out)
    # 4) ST（逐日 PIT）：用 fundamental.is_st
    fund = _fund()
    if require_fundamental:
        st = fund[["date", "symbol", "is_st"]].copy()
        st["symbol"] = st["symbol"].astype(str).str.zfill(6)
        out = out.merge(st, on=["date", "symbol"], how="left")
        # is_st 缺失的视作非 ST（fund 覆盖主板的绝大多数；缺失样本按可购处理）
        out = out[out["is_st"].fillna(False) != True]  # noqa: E712
        out = out.drop(columns=["is_st"])
    # 5) 次新股：上市不满 list_days 天
    ld = list_days if list_days is not None else _list_days_cfg()
    try:
        ld_map = get_list_dates()
        ld_map.index = ld_map.index.astype(str).str.zfill(6)
        ref = out["symbol"].map(ld_map)
        first_visible = out.groupby("symbol")["date"].transform("min")
        list_ref = ref.fillna(first_visible)
        keep = (pd.to_datetime(out["date"]) - pd.to_datetime(list_ref)).dt.days >= ld
        out = out[keep]
    except Exception:
        pass
    return out.drop_duplicates(subset=["date", "symbol"]).reset_index(drop=True)


def position_pool(
    training: pd.DataFrame,
    capital: float = 30_000.0,
    min_turn: float | None = 0.03,
    min_float_mcap: float | None = 3e9,   # 流通市值 >= 30 亿
    max_float_mcap: float | None = None,
    max_lot_frac: float = 0.3,            # 单手价 <= 资本 * 0.3（可一次买 ~3 手/分散）
    min_amount: float = 10_000_000.0,     # 日成交额 >= 1000 万
) -> pd.DataFrame:
    """持仓池：在训练池内按 3 万可负担与流动性再过滤。"""
    df = training.copy()
    fund = _fund()
    if not fund.empty:
        ff = fund[["date", "symbol", "turn", "float_mcap"]].copy()
        ff["symbol"] = ff["symbol"].astype(str).str.zfill(6)
        df = df.merge(ff, on=["date", "symbol"], how="left")
        if min_float_mcap is not None:
            df = df[df["float_mcap"] >= min_float_mcap]
        if max_float_mcap is not None:
            df = df[df["float_mcap"] <= max_float_mcap]
        if min_turn is not None:
            df = df[df["turn"].fillna(0) >= min_turn]
    if max_lot_frac is not None:
        one_lot = df["close"] * 100.0
        df = df[one_lot <= capital * max_lot_frac]
    df = df[df["amount"] >= min_amount]
    drop = [c for c in ("turn", "float_mcap") if c in df.columns]
    return df.drop(columns=drop).reset_index(drop=True)


def build_pools(
    bars: pd.DataFrame,
    capital: float = 30_000.0,
    list_days: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """一键返回 (training_pool, position_pool, stats)。"""
    tr = training_pool(bars, list_days=list_days)
    pos = position_pool(tr, capital=capital)
    stats = {
        "bars": int(len(bars)),
        "training_bars": int(len(tr)),
        "training_symbols": int(tr["symbol"].nunique()) if not tr.empty else 0,
        "training_dates": int(tr["date"].nunique()) if not tr.empty else 0,
        "position_bars": int(len(pos)),
        "position_symbols": int(pos["symbol"].nunique()) if not pos.empty else 0,
    }
    return tr, pos, stats


if __name__ == "__main__":
    store = BarStore()
    bars = store.load(start="2024-06-01", end="2024-06-10")
    tr, pos, st = build_pools(bars, capital=30_000.0)
    print("stats:", st)
    if not tr.empty:
        print("training sample syms:", sorted(tr["symbol"].unique())[:15],
              "n=", tr["symbol"].nunique())
    if not pos.empty:
        print("position sample syms:", sorted(pos["symbol"].unique())[:15],
              "n=", pos["symbol"].nunique())
