from __future__ import annotations

import pandas as pd
import pytest

from quart.portfolio import PortfolioConstructionContext
from quart.strategy import build_strategy
from quart.strategy.index_enhancement import IndexEnhancementStrategy
from tests.test_factor_portfolio_strategy import _market_data


def _exposure_history(md) -> pd.DataFrame:
    rows = []
    for index, symbol in enumerate(md.symbols):
        rows.append({
            "as_of": md.dates[0],
            "available_at": md.dates[0],
            "symbol": symbol,
            "benchmark_weight": 1.0 / len(md.symbols),
            "industry": "全市场",
            "market_cap": float(10 + index),
            "source": "test_csindex",
            "version": "v1",
        })
    return pd.DataFrame(rows)


def test_index_enhancement_uses_pit_benchmark_snapshot(tmp_path):
    md = _market_data()
    history_path = tmp_path / "000300_exposure_history.parquet"
    _exposure_history(md).to_parquet(history_path, index=False)
    strategy = IndexEnhancementStrategy(
        factor_names="vol20_neg,amp20_neg,lottery20_neg",
        top_k=3,
        rebalance_days=1,
        max_weight_pct=0.4,
        industry_active_bound=1.0,
        market_cap_active_bound=10.0,
        exposure_history_path=str(history_path),
    )
    strategy.prepare(md)
    strategy.set_portfolio_context(PortfolioConstructionContext(
        date=md.dates[50], current_weights=pd.Series(dtype=float), equity=1.0, tradable=md.symbols,
    ))

    weights = strategy.target_weights(50)
    receipt = strategy.construction_receipt()

    assert len(weights) == 3
    assert receipt is not None
    assert receipt["exposure_snapshot"] == {
        "benchmark_index": "000300",
        "as_of": str(md.dates[0].date()),
        "available_at": str(md.dates[0].date()),
        "source": "test_csindex",
        "version": "v1",
    }
    assert "industry.全市场" in receipt["constraint_usage"]
    assert "market_cap.active" in receipt["constraint_usage"]


def test_index_enhancement_requires_pit_exposure_history(tmp_path):
    strategy = IndexEnhancementStrategy(exposure_history_path=str(tmp_path / "missing.parquet"))

    with pytest.raises(FileNotFoundError, match="PIT 暴露历史"):
        strategy.prepare(_market_data())


def test_index_enhancement_is_registered_as_factor_strategy():
    strategy = build_strategy("index_enhancement", exposure_history_path="test.parquet")
    assert isinstance(strategy, IndexEnhancementStrategy)

    from quart.strategy.parameters import build_factor_receipt

    receipt = build_factor_receipt("index_enhancement", strategy.params)
    assert receipt["is_factor_strategy"] is True
    assert receipt["controls"]["benchmark_index"] == "000300"
