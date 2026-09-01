"""新因子挖掘（财报 + 涨停事件）的构建逻辑测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.market import MarketData
from quart.research.event_factors import market_limit_sentiment
from scripts.mine_factors import (
    build_financial_factors,
    build_limit_up_factors,
    evaluate_factors,
    evaluate_market_signals,
)


def _md(n: int = 200, n_syms: int = 6, seed: int = 5) -> MarketData:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    rets = pd.DataFrame(
        rng.normal(0.0003, 0.02, size=(n, n_syms)),
        index=dates, columns=[f"{600000 + i:06d}" for i in range(n_syms)],
    )
    closes = (1 + rets).cumprod() * 10
    opens = closes.shift(1).fillna(closes.iloc[0])
    bars = pd.DataFrame({
        "date": np.repeat(dates, n_syms),
        "symbol": np.tile(closes.columns.values, n),
        "open": opens.to_numpy().ravel(),
        "high": np.maximum(opens, closes).to_numpy().ravel() * 1.01,
        "low": np.minimum(opens, closes).to_numpy().ravel() * 0.99,
        "close": closes.to_numpy().ravel(),
        "volume": 1e7,
        "amount": 1e8,
    })
    return MarketData.from_bars(bars)


def _financials(md: MarketData) -> pd.DataFrame:
    """构造季频财务快照。"""
    rows = []
    syms = md.symbols
    dates = pd.date_range("2021-03-31", periods=8, freq="QE")
    for s in syms:
        for i, d in enumerate(dates):
            rows.append({
                "symbol": s, "date": d,
                "roe": 10.0 + i + np.random.rand() * 2,
                "profit_yoy": 5.0 + i * 2,
                "rev_yoy": 3.0 + i,
                "eps": 0.5 + i * 0.1,
                "bps": 3.0 + i * 0.3,
            })
    return pd.DataFrame(rows)


def test_financial_factors_build_shapes():
    md = _md(n=250)
    fin = _financials(md)
    factors = build_financial_factors(fin, md.close_val)
    assert "roe_stability" in factors
    assert "profit_accel" in factors
    assert "earnings_surprise_proxy" in factors
    for f in factors.values():
        assert f.shape[0] == len(md), "因子面板应与行情对齐"
        assert f.shape[1] == len(md.symbols)


def test_financial_factors_no_lookahead():
    """有真实披露时间时必须优先使用，披露前不得看到新一期数据。"""
    md = _md(n=500)
    reports = pd.date_range("2021-03-31", periods=6, freq="QE")
    disclosed_at = [
        pd.Timestamp("2021-05-01"),
        pd.Timestamp("2021-08-01"),
        pd.Timestamp("2021-11-01"),
        pd.Timestamp("2022-04-01"),
        pd.Timestamp("2022-08-01"),
        md.dates[300],
    ]
    fin = pd.DataFrame({
        "symbol": md.symbols[0], "date": reports,
        "published_at": disclosed_at,
        "roe": [10.0, 11.0, 10.5, 11.5, 12.0, 30.0],
        "profit_yoy": [5.0, 6.0, 7.0, 9.0, 10.0, 40.0],
        "rev_yoy": [3.0, 3.5, 4.0, 4.5, 5.0, 8.0],
        "eps": 1.0, "bps": 4.0,
    })
    factors = build_financial_factors(fin, md.close_val)
    assert factors, "测试必须实际生成因子，不能以空字典虚假通过"
    before = md.dates[299]
    after = md.dates[300]
    assert factors["profit_accel"].loc[before, md.symbols[0]] == pytest.approx(1.0)
    assert factors["profit_accel"].loc[after, md.symbols[0]] == pytest.approx(30.0)


def test_limit_up_factors_build():
    md = _md(n=150)
    factors = build_limit_up_factors(md)
    assert set(factors) == {
        "limit_hit_count20_neg",
        "near_limit_count20_neg",
        "speculative_crowding20_neg",
        "crowding_liq20_neg",
        "sector_heat20_neg",
    }
    assert all(panel.shape == (len(md), len(md.symbols)) for panel in factors.values())


def test_limit_up_market_sentiment_is_time_series_not_fake_cross_section():
    md = _md(n=200)
    sentiment = market_limit_sentiment(md)
    assert {"limit_up_count", "limit_up_breadth", "limit_heat_z"} <= set(sentiment)
    assert len(sentiment) == len(md)
    assert not isinstance(sentiment["limit_up_breadth"], pd.DataFrame)


def test_limit_up_candidates_are_past_looking():
    md = _md(n=200)
    factors = build_limit_up_factors(md)
    cutoff = md.dates[120]
    changed = _md(n=200)
    changed.close_val.loc[changed.dates[121]:] *= 2.0
    changed.closes.loc[changed.dates[121]:] *= 2.0
    changed_factors = build_limit_up_factors(changed)
    for name in factors:
        pd.testing.assert_series_equal(
            factors[name].loc[cutoff], changed_factors[name].loc[cutoff], check_names=False
        )


def test_market_signal_evaluation_is_separate_from_cross_section():
    md = _md(n=200)
    result = evaluate_market_signals(market_limit_sentiment(md), md)
    assert isinstance(result, pd.DataFrame)


def test_evaluate_factors_returns_summary():
    """合成 6 只股票不足 300 只流动性门槛 → 结果可能为空，但绝不能崩溃
    （回归：空结果时 sort_values('icir') 曾抛 KeyError）。"""
    md = _md(n=200)
    fin = _financials(md)
    factors = build_financial_factors(fin, md.close_val)
    starts = list(range(100, len(md.dates) - 6, 10))
    result = evaluate_factors(factors, md, starts)
    assert isinstance(result, pd.DataFrame)
    # 空结果可接受（样本不足 300 只），但不崩溃即通过
    if not result.empty:
        assert {"ic", "icir", "pos%", "ls_bp", "n"} <= set(result.columns)


def test_evaluate_factors_aligns_symbol_labels():
    md = _md(n=200)
    factor = pd.DataFrame(
        np.random.default_rng(7).normal(size=(len(md), len(md.symbols))),
        index=md.dates,
        columns=[int(symbol) for symbol in md.symbols],
    )
    # 财务供应商常把代码读成整数；研究入口必须安全对齐并返回空结果，不能崩溃。
    result = evaluate_factors({"supplier_codes": factor}, md, [120, 130])
    assert result.empty


def test_roe_stab_higher_for_stable_company():
    """ROE 稳定的公司 roe_stab 应更高（负标准差取负后更接近 0）。"""
    md = _md(n=300)
    dates = pd.date_range("2021-03-31", periods=8, freq="QE")
    stable, volatile = md.symbols[0], md.symbols[1]
    fin = pd.DataFrame([
        # 稳定公司：ROE 恒定
        {"symbol": stable, "date": d, "roe": 12.0, "profit_yoy": 5.0, "rev_yoy": 3.0,
         "eps": 1.0, "bps": 4.0}
        for d in dates
    ] + [
        # 波动公司：ROE 大幅波动
        {"symbol": volatile, "date": d, "roe": 12.0 + (i % 3) * 20, "profit_yoy": 5.0,
         "rev_yoy": 3.0, "eps": 1.0, "bps": 4.0}
        for i, d in enumerate(dates)
    ])
    factors = build_financial_factors(fin, md.close_val)
    stab = factors["roe_stability"]
    # 稳定公司的 roe_stab（负标准差）应大于波动公司（更接近 0）
    last = stab.index[-1]
    stable_val = stab.loc[last, stable]
    volatile_val = stab.loc[last, volatile]
    # 稳定公司标准差小 → 负值更大（更接近 0）
    assert pd.notna(stable_val) and pd.notna(volatile_val)
    assert stable_val > volatile_val, "ROE 稳定的公司 roe_stab 应更高"
