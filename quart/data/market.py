"""行情面板（MarketData）：回测与信号路径的统一输入。

从 `backtest/engine.py` 迁出：此前 `MarketData` 定义在回测包内，导致
`strategy` 层被迫依赖 `backtest` 层（依赖方向反转）。行情面板是**数据**
概念，不属于回测。
"""
from __future__ import annotations

import pandas as pd

#: 面板字段（宽表：index=date, columns=symbol）
PANEL_FIELDS = ("open", "high", "low", "close", "volume", "amount")


class MarketData:
    """对齐的行情宽表面板。

    Attributes
    ----------
    close_val:
        forward-fill 后的收盘价，用于估值与因子计算。停牌日沿用前值，
        避免把停牌算成 0 收益。
    amounts:
        成交额（元）。缺失时流动性过滤与冲击成本自动关闭。
    """

    def __init__(
        self,
        opens: pd.DataFrame,
        highs: pd.DataFrame,
        lows: pd.DataFrame,
        closes: pd.DataFrame,
        volumes: pd.DataFrame,
        benchmark_close: pd.Series | None = None,
        amounts: pd.DataFrame | None = None,
    ):
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.volumes = volumes
        self.close_val = closes.ffill()
        self.benchmark_close = benchmark_close
        self.amounts = amounts

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.opens.index

    @property
    def symbols(self) -> pd.Index:
        return self.opens.columns

    def __len__(self) -> int:
        return len(self.opens)

    def slice_by_pos(self, lo: int, hi: int) -> "MarketData":
        """按位置切片 [lo, hi)，用于 walk-forward 的 train/test 分段。

        切出来的子面板是**完整**的 MarketData（含全部字段与 benchmark），
        可以直接喂给 BacktestEngine。这是 WFA 能复用同一套回测代码的前提。

        注意：因子类策略的 prepare() 会在子面板上重新计算滚动窗口，
        因此 train 段的因子不会"看到"test 段的数据——这就是防泄漏的机制。
        """
        lo = max(0, int(lo))
        hi = min(len(self), int(hi))
        if hi <= lo:
            raise ValueError(f"empty slice: [{lo}, {hi})")
        return MarketData(
            opens=self.opens.iloc[lo:hi],
            highs=self.highs.iloc[lo:hi],
            lows=self.lows.iloc[lo:hi],
            closes=self.closes.iloc[lo:hi],
            volumes=self.volumes.iloc[lo:hi],
            benchmark_close=(
                self.benchmark_close.iloc[lo:hi] if self.benchmark_close is not None else None
            ),
            amounts=self.amounts.iloc[lo:hi] if self.amounts is not None else None,
        )

    def slice_by_date(self, start=None, end=None) -> "MarketData":
        """按日期切片（两端均含）。"""
        idx = self.dates
        lo = 0 if start is None else int(idx.searchsorted(pd.Timestamp(start), side="left"))
        hi = len(idx) if end is None else int(idx.searchsorted(pd.Timestamp(end), side="right"))
        return self.slice_by_pos(lo, hi)

    @classmethod
    def from_bars(cls, bars: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> "MarketData":
        """从长表 bars 构建面板。

        amount 缺失时填 0：历史数据（尤其部分退市股回填源）可能没有成交额，
        此时流动性过滤与冲击成本自动降级为不生效，而不是直接崩溃。
        """
        bars = bars.sort_values(["date", "symbol"])
        if "amount" not in bars.columns:
            bars = bars.assign(amount=0.0)
        pivots = {
            name: bars.pivot_table(index="date", columns="symbol", values=name, aggfunc="last").sort_index()
            for name in PANEL_FIELDS
        }
        benchmark_close = None
        if benchmark is not None and not benchmark.empty:
            b = benchmark.sort_values("date").set_index("date")["close"]
            benchmark_close = b.reindex(pivots["close"].index)
        return cls(
            opens=pivots["open"],
            highs=pivots["high"],
            lows=pivots["low"],
            closes=pivots["close"],
            volumes=pivots["volume"],
            benchmark_close=benchmark_close,
            amounts=pivots["amount"],
        )


__all__ = ["PANEL_FIELDS", "MarketData"]
