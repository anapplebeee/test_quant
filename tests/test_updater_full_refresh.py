from __future__ import annotations

import pandas as pd

from quart.data import updater


def _bars(symbol: str, date: str = "2026-08-28") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(date)],
            "symbol": [symbol],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [100.0],
            "amount": [1020.0],
        }
    )


class _FakeStore:
    def __init__(self):
        self.saved: list[tuple[str, bool]] = []

    def first_date(self, symbol: str):
        return pd.Timestamp("2020-01-02")

    def last_date(self, symbol: str):
        return pd.Timestamp("2026-08-27")

    def save(self, frame: pd.DataFrame, replace: bool = False):
        self.saved.append((str(frame.iloc[0]["symbol"]), replace))


def test_force_full_refresh_reloads_and_replaces_stock_and_benchmark(monkeypatch):
    store = _FakeStore()
    calls: list[tuple[str, str, str, str]] = []

    def fake_fetch_daily(symbol: str, start: str, end: str, adjust: str):
        calls.append((symbol, start, end, adjust))
        return _bars(symbol)

    monkeypatch.setattr("quart.data.store.BarStore", lambda: store)
    monkeypatch.setattr(updater, "load_config", lambda: {"data": {"sleep_seconds": 0, "adjust": "qfq"}})
    monkeypatch.setattr(updater, "read_hfq_pins", lambda: set())
    monkeypatch.setattr(updater, "fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(updater, "fetch_index_daily", lambda code, start, end: _bars(f"IDX{code}"))
    monkeypatch.setattr("quart.data.quality.load_blocklist", lambda: set())

    stats = updater.update_universe_data(
        "000300",
        ["600519"],
        start="20190101",
        force_full=True,
    )

    assert calls[0][0:2] == ("600519", "20190101")
    assert store.saved == [("600519", True), ("IDX000300", True)]
    assert stats["total"] == 1
    assert stats["ok"] == 1
    assert stats["empty"] == 0
    assert stats["failed"] == 0
    assert stats["refreshed"] == 1


def test_force_full_refresh_keeps_old_stock_when_remote_is_empty(monkeypatch):
    store = _FakeStore()

    monkeypatch.setattr("quart.data.store.BarStore", lambda: store)
    monkeypatch.setattr(updater, "load_config", lambda: {"data": {"sleep_seconds": 0, "adjust": "qfq"}})
    monkeypatch.setattr(updater, "read_hfq_pins", lambda: set())
    monkeypatch.setattr(updater, "fetch_daily", lambda symbol, start, end, adjust: pd.DataFrame())
    monkeypatch.setattr(updater, "fetch_index_daily", lambda code, start, end: pd.DataFrame())
    monkeypatch.setattr("quart.data.quality.load_blocklist", lambda: set())

    stats = updater.update_universe_data("000300", ["600519"], force_full=True)

    assert store.saved == []
    assert stats["empty"] == 1
    assert stats["refreshed"] == 0
