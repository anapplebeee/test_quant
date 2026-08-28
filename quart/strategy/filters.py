from __future__ import annotations

import pandas as pd

from quart.backtest.engine import MarketData


def regime_flat_series(
    bench_close: pd.Series,
    regime_ma: pd.Series,
    band: float = 0.02,
) -> pd.Series:
    """带缓冲带的择时序列（hysteresis）：消除 MA 附近的反复切换。

    无缓冲时 close<MA 的穿越约 26 次/年（2026-08-28 实测 000300），
    每次切换 = 全清/全建仓的双边摩擦。带 2% 缓冲带后降至 ~8 次/年。

    状态机：持仓中需 close < MA*(1-band) 才转空仓；空仓中需 close > MA*(1+band) 才恢复。
    输入为全量序列（prepare 时预计算），保证回测逐日与信号单点调用结果一致。
    """
    flat: list[bool] = []
    state = False
    for c, m in zip(bench_close, regime_ma):
        if pd.isna(m) or pd.isna(c):
            flat.append(state)
            continue
        if state:
            if c > m * (1 + band):
                state = False
        else:
            if c < m * (1 - band):
                state = True
        flat.append(state)
    return pd.Series(flat, index=bench_close.index, name="regime_flat")


def liquidity_mask(md: MarketData, i: int, min_avg_amount: float, days: int = 20) -> pd.Series | None:
    """Return boolean Series over symbols: rolling mean daily turnover >= threshold.

    None means no filter configured (or amount data unavailable).
    """
    if not min_avg_amount or md.amounts is None:
        return None
    lo = max(0, i - days + 1)
    avg = md.amounts.iloc[lo : i + 1].ffill().mean()
    return avg >= float(min_avg_amount)


def apply_liquidity(
    scores: pd.Series,
    md: MarketData,
    i: int,
    min_avg_amount: float,
    days: int = 20,
    min_price: float | None = None,
) -> pd.Series:
    mask = liquidity_mask(md, i, min_avg_amount, days)
    if mask is not None:
        eligible = mask[mask].index
        scores = scores.loc[scores.index.intersection(eligible)]
    if min_price is not None and md.close_val is not None:
        px = md.close_val.iloc[i]
        scores = scores.loc[scores.index.intersection(px[px >= float(min_price)].index)]
    return scores
