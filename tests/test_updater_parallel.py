"""并发数据刷新（updater）测试。

核心不变量：
1. 并发路径产出与串行路径**一致**——并发只是提速，不能改变语义。
2. 限速器只对同一股票生效，不同股票互不阻塞（否则并发退化成串行）。
3. 计数器并发安全，不丢数。
"""
from __future__ import annotations

import threading

import pandas as pd
import pytest

from quart.data.updater import _Throttle, _UpdateCounters, update_universe_data


# ---------------------------------------------------------------- 限速器


def test_throttle_serializes_same_symbol_not_different():
    import time

    t = _Throttle(0.05)

    t("600519")
    t1 = time.monotonic()
    t("600519")
    assert time.monotonic() - t1 >= 0.04, "同一股票未限速"

    t2 = time.monotonic()
    t("000001")
    assert time.monotonic() - t2 < 0.04, "不同股票被错误阻塞"


def test_throttle_disabled_when_interval_zero():
    import time

    t = _Throttle(0.0)
    t1 = time.monotonic()
    for _ in range(5):
        t("600519")
    assert time.monotonic() - t1 < 0.1


def test_throttle_is_thread_safe():
    """多线程同时触发同一股票限速不能损坏内部状态。"""
    t = _Throttle(0.01)
    errors = []

    def worker():
        try:
            for _ in range(50):
                t("600519")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors


# ---------------------------------------------------------------- 计数器


def test_counters_are_thread_safe():
    c = _UpdateCounters()
    threads = [
        threading.Thread(target=lambda: [c.record("ok", False, f"{i}") for _ in range(100)])
        for i in range(8)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert c.ok == 800
    assert c.failed == 0
    assert c.snapshot()[0] == 800


def test_counters_record_statuses():
    c = _UpdateCounters()
    c.record("ok", False, "a")
    c.record("ok", True, "b")
    c.record("empty", False, "c")
    c.record("failed", False, "d")
    d = c.as_dict(4)
    assert d["ok"] == 2
    assert d["refreshed"] == 1
    assert d["empty_symbols"] == ["c"]
    assert d["failed_symbols"] == ["d"]


# ---------------------------------------------------------------- 并发一致


def test_parallel_matches_serial_output(monkeypatch, tmp_path):
    """并发与串行对同一批股票必须产出相同结果。

    方法：monkeypatch fetch_daily 返回确定性数据，分别用 workers=1 和
    workers=4 跑 update_universe_data，比较写入的股票数与保存调用次数。
    """
    # 打桩 fetch_daily：每只股票返回固定的 5 天数据
    calls = {"n": 0, "lock": threading.Lock()}

    def fake_fetch_daily(symbol, start_date, end_date, adjust="qfq"):
        with calls["lock"]:
            calls["n"] += 1
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        return pd.DataFrame({
            "date": dates, "symbol": symbol,
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
            "volume": 1_000_000.0, "amount": 10_000_000.0,
        })

    def fake_fetch_index_daily(code, start, end):
        dates = pd.date_range("2024-01-01", periods=5, freq="B")
        return pd.DataFrame({
            "date": dates, "symbol": f"IDX{code}",
            "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3000.0,
            "volume": 1e8, "amount": 3e11,
        })

    import quart.data.updater as updater
    import quart.data.source_akshare as sas

    monkeypatch.setattr(sas, "fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(sas, "fetch_index_daily", fake_fetch_index_daily)
    monkeypatch.setattr(updater, "fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(updater, "fetch_index_daily", fake_fetch_index_daily)
    # 关闭限速（测试不关心 sleep）
    monkeypatch.setattr(updater, "_Throttle", lambda interval: (lambda s: None))
    # 防止读真实 config 的 sleep
    monkeypatch.setattr(updater, "load_config",
                        lambda: {"data": {"adjust": "qfq", "sleep_seconds": 0.0}})
    monkeypatch.setattr(updater, "read_hfq_pins", lambda: set())

    symbols = [f"{600000 + i:06d}" for i in range(12)]

    # 让 update_universe_data 内部的 BarStore 写到不同目录
    # （BarStore 在函数内 `from quart.data.store import BarStore`，patch store 模块）
    import quart.data.store as store_mod

    ser_store = store_mod.BarStore(root=tmp_path / "ser", partitioned=False)
    monkeypatch.setattr(store_mod, "data_root", lambda: tmp_path / "ser")

    stats_ser = update_universe_data("000300", symbols, start="2024-01-01",
                                     workers=1, full_refresh=True)
    # 每只股票一次 fetch + 一次 index fetch
    assert stats_ser["ok"] == 12, f"串行应全部成功: {stats_ser}"
    assert calls["n"] == 12, "串行每只股票应恰好 fetch 一次"

    n_ser = len(ser_store.symbols())

    par_store = store_mod.BarStore(root=tmp_path / "par", partitioned=False)
    monkeypatch.setattr(store_mod, "data_root", lambda: tmp_path / "par")
    calls["n"] = 0

    stats_par = update_universe_data("000300", symbols, start="2024-01-01",
                                     workers=4, full_refresh=True)
    assert stats_par["ok"] == 12, f"并发应全部成功: {stats_par}"
    assert calls["n"] == 12, "并发每只股票也应恰好 fetch 一次"
    assert len(par_store.symbols()) == n_ser, "并发与串行写入的股票数应一致"


def test_parallel_with_dedup_symbols(monkeypatch, tmp_path):
    """重复符号应去重（dict.fromkeys），并发下不重复拉取。"""
    calls = {"n": 0, "lock": threading.Lock()}

    def fake_fetch_daily(symbol, start_date, end_date, adjust="qfq"):
        with calls["lock"]:
            calls["n"] += 1
        dates = pd.date_range("2024-01-01", periods=3, freq="B")
        return pd.DataFrame({
            "date": dates, "symbol": symbol, "open": 10.0, "high": 10.5,
            "low": 9.5, "close": 10.0, "volume": 1_000_000.0, "amount": 1e7,
        })

    def fake_fetch_index_daily(code, start, end):
        return pd.DataFrame()

    import quart.data.updater as updater
    import quart.data.source_akshare as sas

    monkeypatch.setattr(sas, "fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(sas, "fetch_index_daily", fake_fetch_index_daily)
    monkeypatch.setattr(updater, "fetch_daily", fake_fetch_daily)
    monkeypatch.setattr(updater, "fetch_index_daily", fake_fetch_index_daily)
    monkeypatch.setattr(updater, "_Throttle", lambda interval: (lambda s: None))
    monkeypatch.setattr(updater, "load_config",
                        lambda: {"data": {"adjust": "qfq", "sleep_seconds": 0.0}})
    monkeypatch.setattr(updater, "read_hfq_pins", lambda: set())

    import quart.data.store as store_mod

    monkeypatch.setattr(store_mod, "data_root", lambda: tmp_path)

    # 符号有重复
    stats = update_universe_data("000300", ["600000", "600000", "600001", "600001"],
                                 start="2024-01-01", workers=2, full_refresh=True)
    assert stats["total"] == 2, "重复符号应去重"
    assert calls["n"] == 2, "去重后应只拉 2 次"
    assert stats["ok"] == 2
