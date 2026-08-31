from __future__ import annotations

import numpy as np
import pandas as pd

from quart.data.market import MarketData
from quart.research.factor_audit import FactorInputs, run_factor_audit


def _market(days: int = 340, symbols: int = 12) -> MarketData:
    dates = pd.bdate_range("2024-01-02", periods=days)
    names = [f"{index:06d}" for index in range(symbols)]
    base = np.arange(days, dtype=float)[:, None]
    cross = np.arange(symbols, dtype=float)[None, :]
    daily_returns = 0.0004 + 0.012 * np.sin(base / 7 + cross / 3)
    close = pd.DataFrame(10 * np.exp(np.cumsum(daily_returns, axis=0)), index=dates, columns=names)
    open_ = close * (1 + np.sin(base / 17) * 0.001)
    high = np.maximum(open_, close) * 1.01
    low = np.minimum(open_, close) * 0.99
    volume_values = np.broadcast_to(2_000_000 + cross * 10_000, (days, symbols))
    volume = pd.DataFrame(volume_values, index=dates, columns=names)
    amount = volume * close * 100.0
    return MarketData(open_, high, low, close, volume, amounts=amount)


def test_factor_audit_outputs_stability_and_t1_metadata():
    result = run_factor_audit(
        _market(),
        factor_names=["vol20_neg", "downside_semivol20_neg"],
        min_cross_section=5,
        min_amount=1,
        warmup=260,
    )

    assert set(result.summary["factor"]) == {"vol20_neg", "downside_semivol20_neg"}
    assert {
        "recent_ic",
        "coverage",
        "max_abs_corr",
        "fdr_qvalue",
        "top_turnover",
        "top_median_amount_m",
        "status",
    } <= set(result.summary.columns)
    assert not result.ic_history.empty
    assert result.correlation.shape == (2, 2)
    assert result.metadata["label"] == "signal close T; executable open T+1 to open T+6"
    assert result.metadata["research_status"] == "provisional"
    assert "must not change the live allowlist" in result.metadata["provisional_reason"]
    assert {
        "factor",
        "top_label_cagr",
        "top_label_max_drawdown",
        "annualized_top_turnover",
        "capacity_proxy_m",
        "research_status",
    } <= set(result.baseline.columns)
    assert set(result.baseline["research_status"]) == {"provisional"}


def test_factor_audit_rejects_unknown_factor():
    try:
        run_factor_audit(
            _market(),
            factor_names=["not_a_factor"],
            min_cross_section=5,
            min_amount=1,
            warmup=260,
        )
    except KeyError as exc:
        assert "not_a_factor" in str(exc)
    else:
        raise AssertionError("unknown factor should fail closed")


def test_vwap_factor_respects_volume_in_hands():
    dates = pd.bdate_range("2025-01-02", periods=30)
    columns = ["000001", "000002"]
    close = pd.DataFrame(10.0, index=dates, columns=columns)
    volume = pd.DataFrame(1_000.0, index=dates, columns=columns)
    amount = volume * close * 100.0
    market = MarketData(close, close, close, close, volume, amounts=amount)

    factor = FactorInputs(market).compute("vwap_pos20_neg")

    assert factor is not None
    assert np.allclose(factor.dropna().to_numpy(), 0.0)
