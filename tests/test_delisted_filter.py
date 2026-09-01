"""退市过滤防线测试：清单加载、按退市日裁剪、factor_portfolio 价格防御。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quart.data.delisted import delisted_map, filter_delisted_bars, load_delisted


def test_delisted_map_normalizes_codes_and_dates(tmp_path):
    p = tmp_path / "delisted.parquet"
    pd.DataFrame(
        {
            "code": ["002013", "600999"],
            "name": ["中航机电", "退市股"],
            "delisted_at": ["2023-04-17", "2022-06-30"],
        }
    ).to_parquet(p, index=False)
    mapping = delisted_map(p)
    assert mapping == {
        "002013": pd.Timestamp("2023-04-17"),
        "600999": pd.Timestamp("2022-06-30"),
    }


def test_filter_delisted_bars_keeps_history_drops_post_delisting(tmp_path):
    p = tmp_path / "delisted.parquet"
    pd.DataFrame(
        {
            "code": ["002013"],
            "name": ["中航机电"],
            "delisted_at": ["2023-04-17"],
        }
    ).to_parquet(p, index=False)
    mapping = delisted_map(p)
    bars = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2023-04-10", "2023-04-17", "2023-04-18", "2024-01-05", "2026-08-28"]
            ),
            "symbol": ["002013", "002013", "002013", "002013", "002013"],
            "close": [10.0, 10.5, 9.0, 8.0, 7.0],
        }
    )
    out = filter_delisted_bars(bars, mapping)
    assert len(out) == 1
    assert out["date"].iloc[0] == pd.Timestamp("2023-04-10")
    # 退市日当天及其后全部剔除，退市前保留


def test_filter_delisted_bars_empty_mapping_noop():
    bars = pd.DataFrame({"date": ["2024-01-01"], "symbol": ["000001"], "close": [1.0]})
    out = filter_delisted_bars(bars, {})
    assert len(out) == 1


def test_load_delisted_missing_file_warns_and_returns_empty(tmp_path):
    # 缺失文件不应阻断流程（告警 + 空清单）
    df = load_delisted(tmp_path / "nope.parquet")
    assert df.empty
    assert list(df.columns) == ["code", "name", "delisted_at"]


def test_filter_for_simulation_excludes_delisted_bars(monkeypatch, tmp_path):
    """filter_for_simulation 集成：退市日后的 bar 被剔除。"""
    import quart.data.universe as universe

    p = tmp_path / "delisted.parquet"
    pd.DataFrame(
        {
            "code": ["002013"],
            "name": ["中航机电"],
            "delisted_at": ["2023-04-17"],
        }
    ).to_parquet(p, index=False)
    monkeypatch.setattr("quart.data.delisted.DELISTED_PATH", p)
    bars = pd.DataFrame(
        {
            "date": pd.to_datetime(["2023-04-10", "2023-04-18", "2024-01-05"]),
            "symbol": ["002013", "002013", "002013"],
            "open": [1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0],
            "volume": [1e6, 1e6, 1e6],
            "amount": [1e8, 1e8, 1e8],
        }
    )
    out = universe.filter_for_simulation(
        bars, exclude_star=False, exclude_chinext=False, exclude_st=False
    )
    assert list(out["date"].dt.strftime("%Y-%m-%d")) == ["2023-04-10"]
