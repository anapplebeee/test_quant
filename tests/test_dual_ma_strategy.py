"""dual_ma 修复回归测试：无金叉清仓（FLAT）、超买剔除、金叉新鲜度优先、权重上限。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import MarketData
from quart.execution.constraints import FLAT
from quart.strategy.dual_ma import DualMAStrategy


def _bars(closes: dict[str, np.ndarray]) -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=len(next(iter(closes.values()))))
    close = pd.DataFrame(closes, index=dates)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    rows = []
    for symbol in close.columns:
        rows.extend(
            {
                "date": date,
                "symbol": symbol,
                "open": float(open_.loc[date, symbol]),
                "high": float(high.loc[date, symbol]),
                "low": float(low.loc[date, symbol]),
                "close": float(close.loc[date, symbol]),
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
            }
            for date in dates
        )
    return MarketData.from_bars(pd.DataFrame(rows))


def _make_md(n: int = 60) -> MarketData:
    """确定性路径：
    S0 温和上行（乖离 ~1%）、S1 横盘后末段暴涨（乖离 >15%，超买）、
    S2 微涨震荡（乖离 ~3%）、S3 单边下行（永无金叉）。"""
    t = np.arange(n, dtype=float)
    return _bars(
        {
            "S0": 10.0 * (1 + 0.0015 * t),
            "S1": 10.0 * (1 + 0.03 * np.maximum(0.0, t - 45)),
            "S2": 10.0 * (1 + 0.0008 * t + 0.01 * np.sin(t / 3)),
            "S3": 10.0 * (1 - 0.0025 * t),
        }
    )


def _make_md_all_down(n: int = 60) -> MarketData:
    """全部单边下行：任何时点都没有金叉标的。"""
    t = np.arange(n, dtype=float)
    return _bars({f"S{k}": 10.0 * (1 - (0.002 + 0.0005 * k) * t) for k in range(4)})


def _overshoot(strategy: DualMAStrategy, i: int) -> dict[str, float]:
    fast, slow = strategy.fast_ma.iloc[i], strategy.slow_ma.iloc[i]
    return {
        s: float(fast[s] / slow[s] - 1.0)
        for s in fast.index
        if not pd.isna(fast[s]) and not pd.isna(slow[s]) and fast[s] > slow[s]
    }


def test_dual_ma_flat_when_no_golden_cross():
    """全部下行、无金叉标的 → 明确清仓 {FLAT: 1.0}，而不是返回 {} 死扛持仓。"""
    md = _make_md_all_down()
    strategy = DualMAStrategy(fast_days=5, slow_days=20, max_names=10, max_weight_pct=0.3)
    strategy.prepare(md)
    assert _overshoot(strategy, len(md.dates) - 1) == {}
    assert strategy.target_weights(len(md.dates) - 1) == {FLAT: 1.0}


def test_dual_ma_skips_overbought():
    """S1 末段暴涨导致乖离超过 max_overshoot_pct=0.15 → 被剔除；S0/S2 温和金叉入选。"""
    md = _make_md()
    strategy = DualMAStrategy(
        fast_days=5, slow_days=20, max_names=10, max_weight_pct=0.3, max_overshoot_pct=0.15
    )
    strategy.prepare(md)
    i = len(md.dates) - 1
    weights = strategy.target_weights(i)
    overshoot = _overshoot(strategy, i)
    assert overshoot, "末端应存在金叉标的"
    assert "S1" in overshoot and overshoot["S1"] > 0.15, "S1 应被判定为超买"
    assert "S1" not in weights, "超买标的 S1 不应入选"
    for sym, weight in weights.items():
        assert overshoot[sym] <= 0.15 + 1e-9, f"{sym} 乖离 {overshoot[sym]:.3f} 应被超买剔除"
        assert 0 < weight <= 0.3 + 1e-9


def test_dual_ma_prefers_fresh_cross_and_respects_weight_cap():
    """max_names=2 时按金叉新鲜度（乖离小）优先选出 S0/S2；单票不超上限、总权重 ≤100%。"""
    md = _make_md()
    strategy = DualMAStrategy(
        fast_days=5, slow_days=20, max_names=2, max_weight_pct=0.4, max_overshoot_pct=0.5
    )
    strategy.prepare(md)
    i = len(md.dates) - 1
    weights = strategy.target_weights(i)
    overshoot = _overshoot(strategy, i)
    assert set(weights) == {"S0", "S2"}, f"应选乖离最小的两只，实际 {set(weights)}"
    assert all(0 < w <= 0.4 + 1e-9 for w in weights.values())
    assert sum(weights.values()) <= 1.0 + 1e-9


def test_dual_ma_state_roundtrip():
    strategy = DualMAStrategy(fast_days=5, slow_days=20)
    strategy.prepare(_make_md())
    state = strategy.state_dict()
    assert "next_rebalance" in state
    strategy.load_state_dict({"next_rebalance": 30})
    assert strategy._next_rebalance == 30
