from __future__ import annotations

import pandas as pd

from quart.data.minute_store import MinuteStore


def _mk_bars(n: int = 5, level: str = "5", day: str = "2026-08-25") -> pd.DataFrame:
    ts = pd.date_range(f"{day} 09:35:00", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "ts": ts,
            "level": level,
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": [10.0 + i * 0.1 for i in range(n)],
            "volume": [1000.0] * n,
            "amount": [11000.0 * i + 1 for i in range(n)],
        }
    )


def test_save_load_roundtrip_and_filter(tmp_path):
    store = MinuteStore(tmp_path)
    bars = _mk_bars(level="5")
    n = store.save("000001", bars)
    assert n == len(bars)
    # 幂等：重复写不重复
    n2 = store.save("000001", bars)
    assert n2 == len(bars)
    loaded = store.load("000001", level="5")
    assert len(loaded) == len(bars)
    assert (loaded["ts"].values == bars["ts"].values).all()
    # 按 level 过滤：写 30 不干扰 5
    store.save("000001", _mk_bars(level="30", n=3))
    assert len(store.load("000001", level="5")) == len(bars)
    assert len(store.load("000001", level="30")) == 3
    assert len(store.load("000001")) == len(bars) + 3


def test_load_missing_returns_empty(tmp_path):
    store = MinuteStore(tmp_path)
    out = store.load("999999", level="5")
    assert out.empty
    assert not store.has("999999", "5")


def test_save_requires_level(tmp_path):
    store = MinuteStore(tmp_path)
    df = _mk_bars().drop(columns=["level"])
    try:
        store.save("000001", df)  # 无 level 列也无 level 参数
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_time_range_filter(tmp_path):
    store = MinuteStore(tmp_path)
    store.save("000002", _mk_bars(n=4, level="5", day="2026-08-24"))
    store.save("000002", _mk_bars(n=4, level="5", day="2026-08-25"))
    out = store.load("000002", level="5", start="2026-08-25")
    assert len(out) == 4
    assert all(out["ts"].dt.date.astype(str) == "2026-08-25")
