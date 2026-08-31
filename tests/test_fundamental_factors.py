from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import quart.data.fundamental as fundamental
from quart.data.market import MarketData
from quart.research.factor_audit import FactorInputs, run_factor_audit
from quart.strategy.lowvol_composite import LowVolCompositeStrategy


def _market(days: int = 60, symbols: int = 6) -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=days)
    names = [f"{index:06d}" for index in range(symbols)]
    rng = np.random.default_rng(7)
    close = pd.DataFrame(
        10 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, size=(days, symbols)), axis=0)),
        index=dates,
        columns=names,
    )
    open_ = close * (1 + rng.normal(0, 0.001, size=(days, symbols)))
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume = pd.DataFrame(2_000_000.0, index=dates, columns=names)
    amount = volume * close * 100.0
    return MarketData(open_, high, low, close, volume, amounts=amount)


def _write_fundamental(tmp_path, market: MarketData) -> None:
    """按市场日期/代码生成合成基本面数据：市值随代码递减，换手率随代码递增。"""
    records = []
    for j, symbol in enumerate(market.symbols):
        for date in market.dates:
            records.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "turn": 0.5 + 0.3 * j,
                    "float_mcap": 1e11 / (j + 1),
                    # 第 1 只 PE 为负（亏损），第 2 只 PB 为负（资不抵债）→ 应被掩码
                    "pe_ttm": -10.0 if j == 0 else 10.0 + j,
                    "pb": -1.0 if j == 1 else 1.0 + 0.5 * j,
                    "is_st": False,
                }
            )
    frame = pd.DataFrame(records)
    factor_dir = tmp_path / "factors"
    factor_dir.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(factor_dir / "fundamental_daily.parquet", index=False)


@pytest.fixture()
def fundamental_data(tmp_path, monkeypatch):
    market = _market()
    _write_fundamental(tmp_path, market)
    monkeypatch.setattr(
        fundamental, "fundamental_path", lambda: tmp_path / "factors" / "fundamental_daily.parquet"
    )
    fundamental.load_fundamental.cache_clear()
    yield market
    fundamental.load_fundamental.cache_clear()


def test_size_and_turnover_factors(fundamental_data):
    inputs = FactorInputs(fundamental_data)

    size = inputs.compute("size_neg")
    turn = inputs.compute("turn20_neg")

    assert size is not None and turn is not None
    symbols = list(fundamental_data.symbols)
    row = size.iloc[-1]
    # 市值随代码递减 → -ln 市值随代码递增（小市值得分更高）
    assert row[symbols[0]] < row[symbols[-1]]
    turn_row = turn.iloc[-1]
    # 换手率随代码递增 → 低换手得分随代码递减
    assert turn_row[symbols[0]] > turn_row[symbols[-1]]


def test_value_factors_mask_nonpositive(fundamental_data):
    inputs = FactorInputs(fundamental_data)

    ep = inputs.compute("ep_ttm")
    bp = inputs.compute("bp")

    assert ep is not None and bp is not None
    symbols = list(fundamental_data.symbols)
    assert np.isnan(ep.iloc[-1][symbols[0]])  # PE<=0 不参与价值打分
    assert ep.iloc[-1][symbols[1]] > 0
    assert np.isnan(bp.iloc[-1][symbols[1]])  # PB<=0 不参与价值打分
    assert bp.iloc[-1][symbols[2]] > 0


def test_audit_skips_fundamental_factors_without_data(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fundamental, "fundamental_path", lambda: tmp_path / "missing.parquet"
    )
    fundamental.load_fundamental.cache_clear()
    try:
        result = run_factor_audit(
            _market(),
            factor_names=["vol20_neg", "size_neg"],
            min_cross_section=4,
            min_amount=1,
            warmup=30,
        )
        assert set(result.summary["factor"]) == {"vol20_neg"}
    finally:
        fundamental.load_fundamental.cache_clear()


def test_audit_includes_fundamental_factors(fundamental_data):
    result = run_factor_audit(
        fundamental_data,
        factor_names=["size_neg", "turn20_neg", "ep_ttm", "bp"],
        min_cross_section=4,
        min_amount=1,
        warmup=30,
    )
    assert set(result.summary["factor"]) == {"size_neg", "turn20_neg", "ep_ttm", "bp"}


def _strategy_md(n_days: int = 80):
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    rng = np.random.default_rng(3)
    frames = []
    for symbol in ["000001", "000002", "000003", "000004"]:
        rets = rng.normal(0.0002, 0.01, size=n_days)
        close = (1 + rets).cumprod() * 10
        opens = np.concatenate([[close[0]], close[:-1]])
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": opens,
                    "high": np.maximum(opens, close) * 1.005,
                    "low": np.minimum(opens, close) * 0.995,
                    "close": close,
                    "volume": 1e6,
                    "amount": 1e8,
                }
            )
        )
    return MarketData.from_bars(pd.concat(frames, ignore_index=True))


def test_strategy_blends_size_weight(fundamental_data, tmp_path, monkeypatch):
    md = _strategy_md()
    base = LowVolCompositeStrategy(top_k=2, rebalance_days=5, min_avg_amount=None, use_regime_filter=False)
    base.prepare(md)
    blended = LowVolCompositeStrategy(
        top_k=2, rebalance_days=5, min_avg_amount=None, use_regime_filter=False, size_weight=1.0
    )
    blended.prepare(md)

    assert not base.composite.iloc[-1].equals(blended.composite.iloc[-1])


def test_strategy_skips_blend_when_fundamental_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fundamental, "fundamental_path", lambda: tmp_path / "missing.parquet"
    )
    fundamental.load_fundamental.cache_clear()
    try:
        md = _strategy_md()
        plain = LowVolCompositeStrategy(top_k=2, rebalance_days=5, min_avg_amount=None, use_regime_filter=False)
        plain.prepare(md)
        missing = LowVolCompositeStrategy(
            top_k=2, rebalance_days=5, min_avg_amount=None, use_regime_filter=False, size_weight=1.0
        )
        missing.prepare(md)
        pd.testing.assert_frame_equal(plain.composite, missing.composite)
    finally:
        fundamental.load_fundamental.cache_clear()
