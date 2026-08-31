"""UI-001 前端直读整改：API 层新增函数回归。

覆盖 DR-02~DR-06 的统一入口：数据新鲜度/交易日/股票名/ML 分数、
账户摘要、持仓摘要、配置快照、因子定义。前端页面构建由
test_frontend_build.py 覆盖，本文件只测 API 行为。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

# ---------------- data_api ----------------


def test_get_next_trade_date_returns_iso_and_skips_weekend():
    from api.data_api import get_next_trade_date

    friday = dt.date(2026, 8, 28)  # 周五
    nxt = get_next_trade_date(friday)
    assert nxt is not None
    parsed = dt.date.fromisoformat(nxt)
    assert parsed > friday
    assert parsed.weekday() < 5


def test_get_stock_names_delegates_to_cache(monkeypatch):
    import api.data_api as data_api

    monkeypatch.setattr("common.load_stock_names", lambda: {"600000.SH": "浦发银行"})
    assert data_api.get_stock_names() == {"600000.SH": "浦发银行"}


def test_get_stock_names_degrades_to_empty(monkeypatch):
    import api.data_api as data_api

    def _boom():
        raise RuntimeError("cache broken")

    monkeypatch.setattr("common.load_stock_names", _boom)
    assert data_api.get_stock_names() == {}


def test_get_latest_ml_scores_missing_returns_none(monkeypatch, tmp_path):
    import api.data_api as data_api

    monkeypatch.setattr(data_api, "_scores_path", lambda: tmp_path / "nope.csv")
    assert data_api.get_latest_ml_scores() is None


def test_get_latest_ml_scores_sorted_and_limited(monkeypatch, tmp_path):
    import api.data_api as data_api

    path = tmp_path / "preds.csv"
    pd.DataFrame({
        "datetime": ["2026-08-01", "2026-08-03", "2026-08-02"],
        "symbol": ["A", "B", "C"],
        "score": [0.1, 0.3, 0.2],
    }).to_csv(path, index=False)
    monkeypatch.setattr(data_api, "_scores_path", lambda: path)

    df = data_api.get_latest_ml_scores(limit=2)
    assert df is not None
    assert list(df["symbol"]) == ["B", "C"]  # datetime 降序 + limit


# ---------------- manual_trading_api ----------------


class _FakePosition:
    def __init__(self, symbol: str, qty: int):
        self.symbol = symbol
        self.total_quantity = qty


class _FakeState:
    cash_total = 1000.0
    positions = {
        "600000.SH": _FakePosition("600000.SH", 100),
        "000001.SZ": _FakePosition("000001.SZ", 200),
    }
    total_positions = {"600000.SH": 100, "000001.SZ": 200}


class _FakeRepo:
    def __init__(self, state):
        self._state = state

    def account_state(self, *_args, **_kwargs):
        return self._state


@pytest.fixture
def fake_trading(monkeypatch):
    import api.manual_trading_api as api_mod

    def _patch(state, prices):
        monkeypatch.setattr(api_mod, "repository", lambda: _FakeRepo(state))
        monkeypatch.setattr(api_mod, "load_stock_names", lambda: {"600000.SH": "浦发银行"})
        monkeypatch.setattr(api_mod, "_latest_prices", lambda symbols: prices)

    return _patch


def test_get_account_summary_totals(fake_trading):
    from api.manual_trading_api import get_account_summary

    fake_trading(_FakeState(), {"600000.SH": 10.0})
    out = get_account_summary("2026-08-31")
    assert out == {"cash": 1000.0, "total": 2000.0}


def test_get_account_summary_degrades_when_repo_missing(monkeypatch):
    import api.manual_trading_api as api_mod

    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(api_mod, "repository", _boom)
    assert api_mod.get_account_summary() == {"cash": None, "total": None}


def test_get_holdings_summary_table_and_weights(fake_trading):
    from api.manual_trading_api import get_holdings_summary

    fake_trading(_FakeState(), {"600000.SH": 10.0})
    frame, summary = get_holdings_summary()
    assert summary == {"cash": 1000.0, "equity": 1000.0, "total": 2000.0}
    assert frame is not None and len(frame) == 2
    priced = frame[frame["代码"] == "600000.SH"].iloc[0]
    assert priced["名称"] == "浦发银行"
    assert priced["市值"] == 1000.0
    assert priced["权重%"] == 50.0
    missing = frame[frame["代码"] == "000001.SZ"].iloc[0]
    assert missing["最新价"] == "数据缺失"
    assert missing["权重%"] == "-"


def test_get_holdings_summary_empty_account(fake_trading):
    from api.manual_trading_api import get_holdings_summary

    class _Empty:
        cash_total = 100.0
        positions = {}
        total_positions = {}

    fake_trading(_Empty(), {})
    assert get_holdings_summary() == (None, None)


# ---------------- config_api / research_api ----------------


def test_get_config_snapshot_sections_and_effective():
    from api.config_api import get_config_snapshot
    from quart.config import load_config

    snap = get_config_snapshot()
    assert {"strategy", "risk", "backtest", "manual_trading", "effective"} <= set(snap)
    effective = snap["effective"]
    assert effective["strategy_name"] == load_config()["strategy"]["name"]
    assert isinstance(effective["top_k"], int) and effective["top_k"] > 0
    assert isinstance(effective["rebalance_days"], int) and effective["rebalance_days"] > 0
    assert isinstance(effective["use_regime_filter"], bool)


def test_factor_specs_exposes_registry():
    from api.research_api import factor_specs
    from quart.research.factor_audit import FACTOR_SPECS

    specs = factor_specs()
    assert len(specs) == len(FACTOR_SPECS)
    assert all(hasattr(s, "name") and hasattr(s, "category") for s in specs)
