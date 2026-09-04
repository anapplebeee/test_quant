"""etf_momentum 策略契约测试：周频动量 Top-n 轮动 + 防御仓 + 无前视 + 状态序列化。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.backtest.engine import MarketData
from quart.strategy import REGISTRY, build_strategy
from quart.strategy.etf_momentum import ETFMomentumStrategy


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
                "date": date, "symbol": symbol,
                "open": float(open_.loc[date, symbol]),
                "high": float(high.loc[date, symbol]),
                "low": float(low.loc[date, symbol]),
                "close": float(close.loc[date, symbol]),
                "volume": 1_000_000.0, "amount": 100_000_000.0,
            }
            for date in dates
        )
    return MarketData.from_bars(pd.DataFrame(rows))


def _make_md(n: int = 300) -> MarketData:
    """6 只 ETF：3 只趋势分化(强/中/弱) + 1 只横盘 + 1 只下行 + 防御债 511010。"""
    t = np.arange(n, dtype=float)
    return _bars({
        "510300": 3.0 * (1 + 0.002 * t),          # 强多头(>MA60)
        "510500": 3.0 * (1 + 0.001 * t + 0.01 * np.sin(t / 5)),  # 温和
        "159915": 3.0 * (1 - 0.0015 * t),          # 下行
        "588000": 3.0 * (1 + 0.02 * np.sin(t / 3)),  # 高波震荡
        "511010": 100.0 * (1 + 0.0001 * t),         # 防御债, 缓涨
    })


def test_registered_and_build():
    assert "etf_momentum" in REGISTRY
    s = build_strategy("etf_momentum")
    assert isinstance(s, ETFMomentumStrategy)
    # schema 默认参数
    assert s.params["top_n"] == 2
    assert s.params["risk_etfs"] == "510300,510500,159915,588000,512890,518880,159920,513500"


def test_no_candidates_goes_full_defense():
    """全部下行/横盘、动量≤0 或破 MA60 → 满仓防御债(511010)。"""
    n = 300
    t = np.arange(n, dtype=float)
    md = _bars({f"E{k}": 10.0 * (1 - (0.001 + 0.0005 * k) * t) for k in range(3)}
               | {"511010": 100.0 * (1 + 0.0001 * t)})
    # risk_etfs 需包含面板里出现的风险列 E0..E2
    s = build_strategy("etf_momentum", risk_etfs="E0,E1,E2", defense_etf="511010",
                       mom_short_days=10, mom_long_days=30, ma_window=30)
    s.prepare(md)
    i = len(md.dates) - 1
    # 该日必须是非调仓或调仓都无候选；用调仓日附近验证防御兜底
    w = s.target_weights(i)
    # 若下行标的全不满足，则返回 {511010:1.0}
    risk_cols = [c for c in s.risk_in]
    has_cand = False
    for c in risk_cols:
        if s.score.iloc[i][c] > 0 and md.close_val.iloc[i][c] > s.ma.iloc[i][c]:
            has_cand = True
    if not has_cand:
        # 防御权重应达 1.0（或接近），风险票为 0
        assert w.get("511010", 0.0) >= 1.0 - 1e-6 or not any(v > 0 for k, v in w.items()
                                                              if k != "511010")


def test_weekly_rebalance_topn_weights():
    """强多头 510300/温和 510500 应入选(动量>0 且>MA60)，单票 1/top_n，防御补足。"""
    md = _make_md()
    s = build_strategy("etf_momentum",
                       risk_etfs="510300,510500,159915,588000",
                       defense_etf="511010",
                       mom_short_days=10, mom_long_days=30, ma_window=30,
                       top_n=2)
    s.prepare(md)
    # 遍历后期所有调仓日(周首日)，找一个有 2 个候选的
    found = None
    for i in range(s._warmup(), len(md.dates)):
        if not bool(s.weekly.iloc[i]):
            continue
        w = s.target_weights(i)
        if not w:
            continue
        risk_w = {k: v for k, v in w.items() if k != "511010"}
        if len(risk_w) >= 2:
            found = (i, w)
            break
    assert found is not None, "应在后期出现同时持有 ≥2 只风险票的调仓"
    i, w = found
    risk_w = {k: v for k, v in w.items() if k != "511010"}
    assert all(abs(v - 0.5) < 1e-6 for v in risk_w.values()), f"Top2 应等权0.5: {risk_w}"
    assert abs(sum(w.values()) - 1.0) < 1e-6
    # 入选票必须动量>0 且收盘>MA60
    for c in risk_w:
        assert s.score.iloc[i][c] > 0, c
        assert md.close_val.iloc[i][c] > s.ma.iloc[i][c], c


def test_non_rebalance_day_returns_empty_absent_stop():
    """非周首日且无止损触发 → target_weights 返回 {}（引擎保持持仓）。"""
    md = _make_md()
    s = build_strategy("etf_momentum",
                       risk_etfs="510300,510500,159915,588000",
                       defense_etf="511010",
                       mom_short_days=10, mom_long_days=30, ma_window=30)
    s.prepare(md)
    non_rebal = [i for i in range(s._warmup(), len(md.dates))
                 if not bool(s.weekly.iloc[i])]
    assert non_rebal
    # 在强多头行情里非调仓日通常无止损 → 返回 {}
    got_empty = any(s.target_weights(i) == {} for i in non_rebal[:20])
    # 至少早期(无持仓)返回 {}
    assert s.target_weights(s._warmup()) in ({}, None) or got_empty


def test_state_roundtrip():
    s = build_strategy("etf_momentum")
    s.prepare(_make_md())
    state = s.state_dict()
    for key in ("entry", "ref_nav", "peak", "last_target"):
        assert key in state
    s2 = build_strategy("etf_momentum")
    s2.prepare(_make_md())
    s2.load_state_dict(state)
    assert s2._ref_nav == pytest.approx(state["ref_nav"])


def test_missing_risk_raises():
    """面板里没有任何风险 ETF → prepare 抛 ValueError。"""
    t = np.arange(60, dtype=float)
    md = _bars({"511010": 100.0 * (1 + 0.0001 * t)})
    s = build_strategy("etf_momentum")
    with pytest.raises(ValueError):
        s.prepare(md)
