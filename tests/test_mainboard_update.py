"""主板过滤 + 并发数据刷新参数校验测试。"""
from __future__ import annotations

import re

import pytest

from quart.data.universe import MAINBOARD_PREFIXES, filter_mainboard


# ---------------------------------------------------------------- 主板过滤


def test_mainboard_keeps_only_sh_sz_mainboard_codes():
    codes = [
        "600519",   # 沪主板
        "601318",   # 沪主板
        "603259",   # 沪主板
        "605499",   # 沪主板
        "000001",   # 深主板
        "001979",   # 深主板
        "002594",   # 深主板（原中小板，主板规则）
        "003816",   # 深主板
        "300750",   # 创业板 → 排除
        "301236",   # 创业板 → 排除
        "688111",   # 科创板 → 排除
        "689009",   # 科创板 CDR → 排除
        "830799",   # 北交所 → 排除
        "920002",   # 北交所 → 排除
    ]
    kept = filter_mainboard(codes)
    assert kept == ["000001", "001979", "002594", "003816",
                    "600519", "601318", "603259", "605499"]
    assert "300750" not in kept
    assert "688111" not in kept
    assert "830799" not in kept


def test_mainboard_prefixes_exclude_other_boards():
    assert {"600", "601", "603", "605", "000", "001", "002", "003"} <= set(MAINBOARD_PREFIXES)
    for bad in ("300", "301", "688", "689", "43", "82", "92"):
        assert bad not in MAINBOARD_PREFIXES


def test_mainboard_handles_int_codes():
    # akshare 可能返回 int（去零）或 str
    assert filter_mainboard([600519, 300750, "000001"]) == ["000001", "600519"]


def test_mainboard_empty_and_all_excluded():
    assert filter_mainboard([]) == []
    assert filter_mainboard(["300750", "688111", "830799"]) == []


def test_filter_mainboard_integration_with_fetch(tmp_path, monkeypatch):
    """mainboard 分支正确过滤 fetch_stock_list 的返回。"""
    import quart.data.source_akshare as sas

    def fake():
        import pandas as pd

        return pd.DataFrame({
            "symbol": ["600519", "000001", "300750", "688111", "830799"],
            "name": ["a"] * 5,
        })

    monkeypatch.setattr(sas, "fetch_stock_list", fake)
    codes = filter_mainboard(sas.fetch_stock_list()["symbol"].tolist())
    assert codes == ["000001", "600519"]


# ---------------------------------------------------------------- 并发参数


def test_workers_allowlist_bounds():
    """task_api 的 workers 白名单必须与 updater 的 [1,32] clamp 一致。"""
    from api.task_api import ALLOWED_ARGS

    workers_re = ALLOWED_ARGS["refresh"]["--workers"]
    for ok in (1, 8, 16, 32):
        assert re.fullmatch(workers_re, str(ok)), f"{ok} 应通过 workers 白名单"
    for bad in (0, 33, -1, 100, "abc"):
        assert not re.fullmatch(workers_re, str(bad)), f"{bad} 应被 workers 白名单拒绝"


def test_universe_allowlist_includes_mainboard():
    from api.task_api import ALLOWED_ARGS

    assert re.fullmatch(ALLOWED_ARGS["refresh"]["--universe"], "mainboard")


def test_flag_only_recognizes_full():
    """--full 是无值开关，白名单判定应走 _FLAG_ONLY 分支。"""
    from api.task_api import _FLAG_ONLY

    assert "--full" in _FLAG_ONLY


def test_update_data_has_mainboard_branch():
    """scripts/update_data.py 必须提供 mainboard 选项。"""
    import inspect

    import scripts.update_data as ud

    src = inspect.getsource(ud)
    assert 'choices=["index", "all", "mainboard"]' in src, "update_data 缺少 mainboard 选项"
    assert "filter_mainboard" in src, "update_data 未调用 filter_mainboard"


def test_throttle_serializes_same_symbol_not_different():
    """限速器必须只对同一股票限速，不同股票互不阻塞。"""
    import time

    from quart.data.updater import _Throttle

    t = _Throttle(0.05)

    # 同一股票第二次调用应等待
    t("600519")
    t1 = time.monotonic()
    t("600519")
    assert time.monotonic() - t1 >= 0.04, "同一股票未限速"

    # 不同股票应立即通过
    t2 = time.monotonic()
    t("000001")
    assert time.monotonic() - t2 < 0.04, "不同股票被错误阻塞"


def test_update_counters_thread_safe():
    """计数器并发记录不丢数（验证 _UpdateCounters 锁正确）。"""
    import threading

    from quart.data.updater import _UpdateCounters

    c = _UpdateCounters()
    threads = [
        threading.Thread(target=lambda: [c.record("ok", False, f"{i}") for _ in range(100)])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.ok == 800
    assert c.failed == 0
