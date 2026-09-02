from __future__ import annotations

import json

import pandas as pd
import pytest

from quart.data.source_zhitu import (
    ZhituSource,
    exchange_suffix,
    fetch_minute_kline,
    _API_BASE,
)


def test_exchange_suffix_mapping():
    assert exchange_suffix("600519") == "SH"
    assert exchange_suffix("000001") == "SZ"
    assert exchange_suffix("300750") == "SZ"
    assert exchange_suffix("688001") == "SH"
    assert exchange_suffix("830001") == "BJ"


def test_token_required_if_not_injected(monkeypatch):
    monkeypatch.delenv("ZHITU_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ZHITU_API_TOKEN"):
        ZhituSource(token=None)


def test_fetch_minute_parses_rows(monkeypatch):
    monkeypatch.setenv("ZHITU_API_TOKEN", "test-token")
    captured = {}

    def fake_get(self, url, timeout=20.0):
        captured["url"] = url
        return [
            {"t": "2026-08-25 09:35:00", "o": 10.0, "h": 10.5, "l": 9.8, "c": 10.2,
             "v": 1234, "a": 12500000, "pc": 10.0},
            {"t": "2026-08-25 09:40:00", "o": 10.2, "h": 10.6, "l": 10.1, "c": 10.5,
             "v": 900, "a": 9500000, "pc": 10.0},
        ]

    monkeypatch.setattr(ZhituSource, "_get", fake_get)
    src = ZhituSource(token="test-token")
    df = src.fetch_minute_kline("000001", "5", "2026-08-25", "2026-08-25")
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2
    assert df["ts"].iloc[0] == pd.Timestamp("2026-08-25 09:35:00")
    assert df["volume"].iloc[0] == 1234
    assert "000001.SZ" in captured["url"]
    assert "/5/n" in captured["url"]
    assert "st=20260825&et=20260825" in captured["url"]


def test_fetch_minute_returns_empty_before_history_start(monkeypatch):
    monkeypatch.setenv("ZHITU_API_TOKEN", "test-token")

    def boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("should not hit network before history start")

    monkeypatch.setattr(ZhituSource, "_get", boom)
    src = ZhituSource(token="test-token")
    df = src.fetch_minute_kline("600000", "5", "2020-01-01", "2020-06-01")
    assert df.empty  # end < MINUTE_HISTORY_START -> 不请求直接空


def test_module_level_fetch(monkeypatch):
    monkeypatch.setenv("ZHITU_API_TOKEN", "test-token")

    def fake_get(self, url, timeout=20.0):
        return []

    monkeypatch.setattr(ZhituSource, "_get", fake_get)
    df = fetch_minute_kline("600519", "5", "2026-08-25", "2026-08-25", token="test-token")
    assert df.empty
