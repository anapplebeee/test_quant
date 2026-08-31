from __future__ import annotations

from itertools import pairwise

import pandas as pd

from quart.data.source_akshare import _tencent_date_chunks


def test_tencent_date_chunks_are_non_overlapping_two_year_windows():
    chunks = _tencent_date_chunks("20190101", "20260831")

    assert chunks == [
        ("2019-01-01", "2020-12-31"),
        ("2021-01-01", "2022-12-31"),
        ("2023-01-01", "2024-12-31"),
        ("2025-01-01", "2026-08-31"),
    ]
    for previous, current in pairwise(chunks):
        assert pd.Timestamp(previous[1]) + pd.offsets.Day(1) == pd.Timestamp(current[0])


def test_tencent_date_chunks_handle_empty_range():
    assert _tencent_date_chunks("20260102", "20260101") == []
