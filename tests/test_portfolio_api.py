from __future__ import annotations

import numpy as np
import pandas as pd

from api import portfolio_api


def test_latest_monthly_returns_compounds(monkeypatch):
    equity = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
        "equity": [100.0, 110.0, 121.0],
    })

    monkeypatch.setattr(
        portfolio_api,
        "_latest_artifact_table",
        lambda prefix, name: equity if prefix == "backtest_" and name == "equity" else None,
    )

    monthly = portfolio_api.latest_monthly_returns()

    assert monthly.loc[2026, 1] == 21.0


def test_portfolio_factor_exposure_uses_real_bars(monkeypatch):
    dates = pd.bdate_range("2026-01-02", periods=70)
    bars = []
    for index, symbol in enumerate(("000001", "000002"), 1):
        returns = 0.001 * index + 0.004 * index * np.sin(np.arange(len(dates)) / 5)
        close = 10 * np.exp(np.cumsum(returns))
        bars.extend(
            {"date": date, "symbol": symbol, "close": value}
            for date, value in zip(dates, close, strict=True)
        )
    frame = pd.DataFrame(bars)
    monkeypatch.setattr(portfolio_api, "current_holdings", lambda: ({"000001": 100, "000002": 200}, 0.0))
    monkeypatch.setattr(portfolio_api, "holding_bars", lambda symbols: frame)

    exposure = portfolio_api.portfolio_factor_exposure()

    assert {"动量(mom60)", "波动率(vol20)", "规模(持仓市值)"} <= set(exposure["因子"])
    assert list(exposure.columns) == portfolio_api.EXPOSURE_COLUMNS


def test_industry_trade_summary(monkeypatch):
    trades = pd.DataFrame({
        "symbol": [1, 2, 1],
        "side": ["BUY", "BUY", "SELL"],
        "amount": [100.0, 200.0, 40.0],
    })
    industries = pd.Series({"000001": "银行", "000002": "科技"})
    monkeypatch.setattr(portfolio_api, "_latest_artifact_table", lambda prefix, name: trades)
    monkeypatch.setattr(portfolio_api, "_industry_map", lambda: industries)

    result = portfolio_api.latest_industry_trade_summary().set_index("行业")

    assert result.loc["银行", "买入"] == 100.0
    assert result.loc["银行", "卖出"] == 40.0
