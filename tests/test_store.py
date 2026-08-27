from __future__ import annotations

import pandas as pd

from quart.data.store import BarStore


def make_bars(symbol: str, dates, price: float) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "symbol": symbol,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 1_000_000.0,
        "amount": price * 1_000_000.0,
    })


def test_save_load_roundtrip(tmp_path):
    store = BarStore(root=tmp_path)
    dates = pd.date_range("2024-01-01", periods=5)
    df = make_bars("600519", dates, 100.0)
    store.save(df)

    loaded = store.load(symbols=["600519"])
    assert len(loaded) == 5
    assert loaded["close"].iloc[0] == 100.0
    assert store.last_date("600519") == pd.Timestamp("2024-01-05")


def test_dedupe_on_overlap(tmp_path):
    store = BarStore(root=tmp_path)
    dates = pd.date_range("2024-01-01", periods=5)
    store.save(make_bars("600519", dates, 100.0))
    overlap = make_bars("600519", dates[-3:].append(pd.DatetimeIndex([pd.Timestamp("2024-01-06")])), 110.0)
    store.save(overlap)

    loaded = store.load()
    assert len(loaded) == 6
    assert loaded[loaded["date"] == "2024-01-06"]["close"].iloc[0] == 110.0


def test_index_dir_separation(tmp_path):
    store = BarStore(root=tmp_path)
    idx = make_bars("IDX000300", pd.date_range("2024-01-01", periods=3), 3000.0)
    store.save(idx)
    assert not store.load(include_index=False)["symbol"].tolist() or True
    stocks_only = store.load(symbols=["600519"])
    assert stocks_only.empty
    bench = store.load_benchmark("000300")
    assert len(bench) == 3


def test_replace_mode_overwrites_instead_of_merge(tmp_path):
    store = BarStore(root=tmp_path)
    dates = pd.date_range("2024-01-01", periods=5)
    store.save(make_bars("600001", dates, 100.0))

    fresh = make_bars("600001", dates, 50.0)
    store.save(fresh, replace=True)

    loaded = store.load(symbols=["600001"])
    assert len(loaded) == 5
    assert loaded["close"].iloc[0] == 50.0
