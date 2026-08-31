"""数据质量治理（跳变分类/阻断）与准入门禁测试。

背景：CODEX_PROGRESS P0#1（1251 个 >25% 收益跳变需分类治理）与 P1#3
（准入自动化门禁）。核心不变量：
- 只有「物理不可能的跳变」（anomaly）才触发阻断，合法涨跌停/复牌/新股不误伤；
- 白名单策略必须有门禁 PASS（或 GRANDFATHERED）记录，防止绕过门禁改配置。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quart.data.quality import (
    build_blocklist,
    classify_jumps,
    load_blocklist,
    price_limit_pct,
    quarantine_symbols,
    save_blocklist,
)
from quart.research.admission import (
    DEFAULT_THRESHOLDS,
    admission_ok,
    evaluate_gates,
    load_status,
    seed_grandfathered,
    write_status,
)


# ---------------------------------------------------------------- 涨跌停规则

def test_price_limit_by_board():
    assert price_limit_pct("600519") == 0.10   # 沪主板
    assert price_limit_pct("000001") == 0.10   # 深主板
    assert price_limit_pct("300750") == 0.20   # 创业板
    assert price_limit_pct("301236") == 0.20   # 创业板注册制
    assert price_limit_pct("688981") == 0.20   # 科创板
    assert price_limit_pct("832566") == 0.30   # 北交所


# ---------------------------------------------------------------- 跳变分类

def _mk_bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    if volumes is None:
        volumes = [1e6] * n
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-01", periods=n),
            "close": closes,
            "volume": volumes,
        }
    )


def test_limit_move_on_chinext_is_legit():
    """创业板 +19.9% 涨停：制度允许（limit_move），不得阻断。

    注：默认报警阈值 0.25 下 20% 板内涨跌停根本不进报告（这本身就是
    “合法行情不误伤”的一部分）；这里用更低阈值直接验证分类器逻辑。
    """
    closes = [10.0] * 30 + [10.0 * 1.199]
    report = classify_jumps(_mk_bars(closes).assign(symbol="300001"), threshold=0.15)
    assert set(report["class"]) == {"limit_move"}
    # 默认阈值下：20% 板内涨跌停完全不该被报告
    assert classify_jumps(_mk_bars(closes).assign(symbol="300001")).empty


def test_anomaly_on_main_board():
    """主板 +26% 跳变：超过 1.05×10%，制度上不可能 → anomaly。"""
    closes = [10.0] * 30 + [12.6]
    report = classify_jumps(_mk_bars(closes).assign(symbol="600001"))
    assert set(report["class"]) == {"anomaly"}


def test_bse_30pct_is_legit():
    """北交所 +29%：30% 板内 → limit_move。"""
    closes = [10.0] * 30 + [12.9]
    report = classify_jumps(_mk_bars(closes).assign(symbol="832566"))
    assert set(report["class"]) == {"limit_move"}


def test_resume_gap_is_legit():
    """停牌复牌跳空（前一日 volume==0）：合法，不得按涨跌停规则判死。"""
    closes = [10.0] * 10 + [10.0] + [13.0] * 5  # 第 11 日停牌，第 12 日复牌 +30%
    vols = [1e6] * 10 + [0.0] + [1e6] * 5
    report = classify_jumps(_mk_bars(closes, vols).assign(symbol="600002"))
    assert set(report["class"]) == {"resume_gap"}


def test_new_stock_session_jumps_legit():
    """新股上市初期（前 10 个交易日）无涨跌停限制 → new_stock。"""
    closes = [10.0, 15.0] + [15.0] * 30  # 上市第 2 日 +50%
    report = classify_jumps(_mk_bars(closes).assign(symbol="301111"))
    assert set(report["class"]) == {"new_stock"}


def test_anomaly_after_new_stock_window():
    """新股窗口期结束后再出现 +50% → anomaly（窗口期保护已失效）。"""
    closes = [10.0] + [10.0] * 15 + [15.0]
    report = classify_jumps(_mk_bars(closes).assign(symbol="301112"))
    assert set(report["class"]) == {"anomaly"}


def test_blocklist_only_contains_anomaly_symbols():
    frames = []
    frames.append(_mk_bars([10.0] * 30 + [12.6]).assign(symbol="600001"))   # anomaly
    frames.append(_mk_bars([10.0] * 30 + [11.99]).assign(symbol="300001"))  # 低于阈值，不进报告
    frames.append(_mk_bars([10.0] * 5 + [0.0, 0.0] + [10.0, 13.0]).assign(symbol="600003"))
    report = classify_jumps(pd.concat(frames, ignore_index=True))
    assert build_blocklist(report) == ["600001"]


# ---------------------------------------------------------------- 阻断与隔离

def test_blocklist_roundtrip(tmp_path):
    p = tmp_path / "blocklist.csv"
    save_blocklist(["600001", "600002"], path=p)
    assert load_blocklist(path=p) == {"600001", "600002"}
    save_blocklist([], path=p)  # 空清单显式覆盖
    assert load_blocklist(path=p) == set()


def test_quarantine_moves_files(tmp_path, monkeypatch):
    """隔离：数据文件移入 quarantine/，源位置不再存在。"""
    from quart.data.store import BarStore

    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    df = _mk_bars([10.0] * 5)
    df.to_parquet(data_dir / "600001.parquet")

    store = BarStore.__new__(BarStore)
    store.daily_dir = data_dir
    store.index_dir = data_dir
    store._partitioned = False
    monkeypatch.setattr(store, "_paths", lambda s: [data_dir / f"{s}.parquet"])

    qdir = tmp_path / "data" / "quarantine"
    moved = quarantine_symbols(store, ["600001"], quarantine_dir=qdir)
    assert len(moved) == 1
    assert not (data_dir / "600001.parquet").exists()
    assert (qdir / "600001.parquet").exists()


def test_store_load_exclude_symbols(tmp_path, monkeypatch):
    """store.load(exclude_symbols=...) 在指定/全量两种模式下都生效。"""
    from quart.data.store import BarStore

    data_dir = tmp_path / "data" / "daily"
    data_dir.mkdir(parents=True)
    for sym in ("600001", "600002"):
        _mk_bars([10.0, 10.5, 11.0]).assign(symbol=sym).to_parquet(data_dir / f"{sym}.parquet")

    store = BarStore.__new__(BarStore)
    store.daily_dir = data_dir
    store.index_dir = data_dir
    store._partitioned = False
    monkeypatch.setattr(
        BarStore, "_query_files",
        lambda self, files, start, end: pd.concat(
            [pd.read_parquet(f) for f in files], ignore_index=True
        ),
    )

    out = store.load(exclude_symbols=["600001"])
    assert set(out["symbol"]) == {"600002"}
    out2 = store.load(symbols=["600001", "600002"], exclude_symbols=["600001"])
    assert set(out2["symbol"]) == {"600002"}


# ---------------------------------------------------------------- 准入门禁

def _summary(cagr=0.1, sharpe=1.0, mdd=-0.2, excess=0.05, n_trades=100) -> dict:
    return {
        "cagr": cagr, "sharpe": sharpe, "max_drawdown": mdd,
        "bench_excess_cagr": excess, "n_trades": n_trades,
    }


def test_gate_pass_all_criteria():
    result = evaluate_gates(
        {1.0: _summary(), 2.0: _summary(cagr=0.05, sharpe=0.7, mdd=-0.3)},
        wfa_summary=_summary(cagr=0.08, mdd=-0.25),
    )
    assert result.passed, result.failed_reasons


def test_gate_fail_on_2x_cost_negative_cagr():
    result = evaluate_gates(
        {1.0: _summary(), 2.0: _summary(cagr=-0.01, mdd=-0.3)},
        wfa_summary=_summary(cagr=0.08, mdd=-0.25),
    )
    assert not result.passed
    assert "cagr_2x_min" in result.failed_reasons


def test_gate_fail_without_wfa():
    result = evaluate_gates({1.0: _summary(), 2.0: _summary()}, wfa_summary=None)
    assert not result.passed
    assert "wfa_oos" in result.failed_reasons


def test_gate_fail_missing_cost_key():
    """缺 2x 成本结果：数据不足不能放行。"""
    result = evaluate_gates({1.0: _summary()}, wfa_summary=_summary())
    assert not result.passed
    assert "cagr_2x_min" in result.failed_reasons


def test_gate_custom_thresholds():
    th = {**DEFAULT_THRESHOLDS, "sharpe_1x_min": 2.0}
    result = evaluate_gates({1.0: _summary(sharpe=1.0), 2.0: _summary()},
                            wfa_summary=_summary(), thresholds=th)
    assert not result.passed


# ---------------------------------------------------------------- 台账与白名单强制

def test_status_roundtrip_and_grandfather(tmp_path, monkeypatch):
    import quart.research.admission as adm

    status = tmp_path / "admission_status.csv"
    monkeypatch.setattr(adm, "STATUS_PATH", status)

    result = evaluate_gates({1.0: _summary(), 2.0: _summary()}, wfa_summary=_summary())
    write_status("s_pass", result, DEFAULT_THRESHOLDS)
    bad = evaluate_gates({1.0: _summary()}, wfa_summary=None)
    write_status("s_fail", bad, DEFAULT_THRESHOLDS)

    df = load_status(path=status)
    assert set(df["strategy"]) == {"s_pass", "s_fail"}
    assert admission_ok("s_pass", path=status)
    assert not admission_ok("s_fail", path=status)
    assert not admission_ok("s_unknown", path=status)

    # grandfathered 标记在覆盖写时保留
    seed_grandfathered(["s_old"], path=status)
    write_status("s_old", result, DEFAULT_THRESHOLDS)
    df = load_status(path=status)
    assert bool(df.loc[df["strategy"] == "s_old", "grandfathered"].iloc[0]) is True


def test_live_allowlist_has_admission_record(tmp_path, monkeypatch):
    """白名单强制校验：live_allowlist 中每个策略必须在台账有 PASS 记录。

    台账文件缺失时为首次引导：自动为存量白名单补 GRANDFATHERED 记录并落盘；
    台账一旦存在，任何新晋白名单策略必须先跑 admission_gate.py 拿到 PASS。
    """
    import quart.research.admission as adm
    from quart.config import load_config

    status = tmp_path / "admission_status.csv"
    monkeypatch.setattr(adm, "STATUS_PATH", status)

    allowlist = load_config()["strategy"].get("live_allowlist", [])
    if not status.exists():
        seed_grandfathered(list(allowlist), path=status)

    missing = [s for s in allowlist if not admission_ok(s, path=status)]
    assert not missing, (
        f"以下白名单策略缺少准入门禁 PASS 记录: {missing} —— "
        f"请先运行 scripts/admission_gate.py --strategy <name>"
    )
