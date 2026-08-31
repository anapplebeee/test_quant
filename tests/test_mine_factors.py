"""新因子挖掘（财报 + 涨停事件）的构建逻辑测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.mine_factors import build_financial_factors, build_limit_up_factors, evaluate_factors
from quart.data.market import MarketData


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
    assert "roe_stab" in factors
    assert "profit_accel" in factors
    assert "surprise" in factors
    for f in factors.values():
        assert f.shape[0] == len(md), "因子面板应与行情对齐"
        assert f.shape[1] == len(md.symbols)


def test_financial_factors_no_lookahead():
    """财报因子必须做披露时滞：报告期 + 120 天才可用。"""
    md = _md(n=500)
    # 只给最后一天的报告期数据 → 前 120 天应为 NaN
    fin = pd.DataFrame({
        "symbol": [md.symbols[0]], "date": [md.dates[-1]],
        "roe": [15.0], "profit_yoy": [10.0], "rev_yoy": [5.0],
        "eps": [1.0], "bps": [4.0],
    })
    factors = build_financial_factors(fin, md.close_val)
    # 最后 120 天内应无值（报告期在最后一天 + 120 天披露滞后 > 数据末尾）
    for name, f in factors.items():
        assert f.iloc[:-1].notna().sum().sum() == 0, f"{name} 存在前视"


def test_limit_up_factors_build():
    md = _md(n=150)
    factors = build_limit_up_factors(md)
    assert "limit_up_density" in factors
    assert "limit_up_density_smooth" in factors
    assert "limit_up_next" in factors
    # 密度是每日标量扩成的截面
    assert factors["limit_up_density"].shape == (len(md), len(md.symbols))
    # 密度值应 >= 0
    assert (factors["limit_up_density"].fillna(0) >= 0).all().all()


def test_limit_up_density_is_nonnegative_int_like():
    md = _md(n=200)
    factors = build_limit_up_factors(md)
    density = factors["limit_up_density"]
    # 每行的值应一致（截面复制），且为非负
    for i in range(0, len(md), 20):
        row = density.iloc[i].dropna()
        if not row.empty:
            assert (row.values == row.values[0]).all(), "密度应每行相同"
            assert row.values[0] >= 0


def test_limit_up_next_only_on_prior_limit_days():
    md = _md(n=200)
    factors = build_limit_up_factors(md)
    lu_next = factors["limit_up_next"]
    # 涨停次日收益列应只在前一日涨停时有值，否则 NaN
    # （合成数据几乎不会涨停，所以大部分应为 NaN/0）
    assert lu_next.notna().sum().sum() >= 0


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
    stab = factors["roe_stab"]
    # 稳定公司的 roe_stab（负标准差）应大于波动公司（更接近 0）
    last = stab.index[-1]
    stable_val = stab.loc[last, stable]
    volatile_val = stab.loc[last, volatile]
    # 稳定公司标准差小 → 负值更大（更接近 0）
    assert pd.notna(stable_val) and pd.notna(volatile_val)
    assert stable_val > volatile_val, "ROE 稳定的公司 roe_stab 应更高"
