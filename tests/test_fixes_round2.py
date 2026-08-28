"""全栈体检修复的回归测试（2026-08-28）。"""
from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import safe_path, valid_date8, valid_name, valid_symbol


# ---------- F3: 路径穿越防护 ----------

def test_validators():
    assert valid_symbol("000001") and valid_symbol("600519") and valid_symbol("300750")
    assert not valid_symbol("../etc") and not valid_symbol("1") and not valid_symbol("000001#.py")
    assert valid_date8("20260828") and not valid_date8("2026-08-28") and not valid_date8("../../x")
    assert valid_name("lowvol_composite") and not valid_name("../secret")


def test_safe_path_blocks_traversal(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    ok = safe_path(base, "daily", "000001.parquet")
    assert ok is not None and str(ok).startswith(str(base.resolve()))
    assert safe_path(base, "..", "..", "etc", "passwd") is None
    assert safe_path(base, "..\\..\\secret.txt") is None


# ---------- F5/F6: 任务系统 ----------

@pytest.fixture()
def tq(tmp_path, monkeypatch):
    """隔离的任务队列：TASKS 指向临时脚本"""
    import api.task_api as ta

    quick = tmp_path / "quick_task.py"
    quick.write_text("print('hello from task')\n", encoding="utf-8")
    slow = tmp_path / "slow_task.py"
    slow.write_text("import time\nprint('start')\ntime.sleep(60)\n", encoding="utf-8")

    monkeypatch.setattr(ta, "TASKS", {
        "quick": {"name": "快速任务", "script": str(quick), "args": [], "resource": "compute"},
        "slow": {"name": "慢任务", "script": str(slow), "args": [], "resource": "compute"},
    })
    queue = ta.TaskQueue(max_history=3)
    monkeypatch.setattr(ta, "task_queue", queue)
    yield ta, queue
    queue._shutdown = True


def test_submit_returns_instance_id_and_callbacks_fire(tq):
    ta, queue = tq
    done = []
    ok, msg, iid1 = queue.submit("quick", on_complete=lambda tid, code: done.append((tid, code)))
    assert ok and iid1 == "quick"
    _wait_completed(queue, "quick", 1)
    ok, msg, iid2 = queue.submit("quick", on_complete=lambda tid, code: done.append((tid, code)))
    assert ok and iid2 == "quick#2", "首个完成后再次提交应获得 #2 实例 ID（修复前此场景 UI 死循环）"

    deadline = time.time() + 30
    while time.time() < deadline and len(done) < 2:
        time.sleep(0.2)
    assert len(done) == 2, "两个实例的完成回调都必须触发（修复前排队任务收不到事件）"
    assert {tid for tid, _ in done} == {"quick", "quick#2"}
    for tid, code in done:
        assert queue.tasks[tid].status.value == "completed"
        assert code == 0


def test_duplicate_submit_rejected(tq):
    _, queue = tq
    ok, _, _ = queue.submit("slow")
    assert ok
    ok2, msg2, _ = queue.submit("slow")
    assert not ok2 and "已在队列" in msg2


def _wait_completed(queue, family, count: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = sum(1 for t in queue.tasks.values()
                if t.family == family and t.status.value == "completed")
        if n >= count:
            return
        time.sleep(0.2)
    raise AssertionError(f"等待 {family} 完成 {count} 个实例超时")


def test_cancel_running_task_kills_and_keeps_status(tq):
    ta, queue = tq
    ok, _, iid = queue.submit("slow")
    assert ok
    deadline = time.time() + 20
    while time.time() < deadline:
        t = queue.tasks[iid]
        if t.status.value == "running" and t.process:
            break
        time.sleep(0.2)
    assert queue.tasks[iid].process is not None, "任务应在 20s 内进入运行态"

    ok_c, msg_c = queue.cancel(iid)
    assert ok_c, msg_c
    deadline = time.time() + 15
    while time.time() < deadline and queue.tasks[iid].process:
        time.sleep(0.2)
    proc = queue.tasks[iid].process
    assert proc is None or proc.poll() is not None, "进程应被终止（Windows 下 taskkill /T 杀进程树）"
    assert queue.tasks[iid].status.value == "cancelled", "取消后状态不得被覆盖回 completed/failed"


def test_timeout_watchdog_kills_task(tq):
    ta, queue = tq
    # TASKS 模板加 2 秒超时：慢任务应在超时后被强杀且状态为 failed
    ta.TASKS["slow"]["timeout"] = 2
    ok, _, iid = queue.submit("slow")
    assert ok
    deadline = time.time() + 30
    while time.time() < deadline:
        t = queue.tasks[iid]
        if t.status.value in ("failed", "cancelled", "completed"):
            break
        time.sleep(0.3)
    t = queue.tasks[iid]
    assert t.status.value == "failed", f"超时任务应为 failed，实际 {t.status.value}"
    assert any("强制终止" in line for line in t.output_lines), "输出应包含超时强杀提示"
    assert t.returncode not in (0, None)


def test_min_list_days_filter(monkeypatch):
    """次新股过滤：用全历史上市首日判断，剔除上市不满 N 天的行情行"""
    import pandas as pd

    from quart.data import universe as u

    monkeypatch.setattr(
        u, "get_list_dates",
        lambda force_refresh=False: pd.Series(
            {"000001": pd.Timestamp("2015-01-01"),   # 老股
             "000002": pd.Timestamp("2021-06-01")},  # 窗口内新上市
            name="first_date",
        ),
    )
    bars = pd.DataFrame({
        "symbol": ["000001"] * 3 + ["000002"] * 4,
        "date": pd.to_datetime([
            "2021-05-01", "2021-08-01", "2021-12-01",
            "2021-06-15", "2021-09-01", "2021-10-01", "2022-01-01",
        ]),
    })
    out = u.filter_for_simulation(
        bars, exclude_star=False, exclude_chinext=False,
        exclude_st=False, min_list_days=120,
    )
    # 000001 全保留；000002 上市 120 天内（06-15、09-01）被剔除，10-01 起保留
    assert (out["symbol"] == "000001").sum() == 3
    kept_b = sorted(out.loc[out["symbol"] == "000002", "date"].dt.strftime("%Y-%m-%d"))
    assert kept_b == ["2021-10-01", "2022-01-01"]

    # 窗口内首日回退：不在名单里的 symbol 以窗口首日为上市日
    bars2 = pd.DataFrame({
        "symbol": ["999999"] * 2,
        "date": pd.to_datetime(["2021-01-01", "2021-06-01"]),
    })
    monkeypatch.setattr(
        u, "get_list_dates",
        lambda force_refresh=False: pd.Series({"000001": pd.Timestamp("2015-01-01")}, name="first_date"),
    )
    out2 = u.filter_for_simulation(
        bars2, exclude_star=False, exclude_chinext=False,
        exclude_st=False, min_list_days=120,
    )
    assert out2["date"].tolist() == [pd.Timestamp("2021-06-01")]


def test_history_trim(tq):
    ta, queue = tq
    # max_history=3：依次完成 5 个小任务后，tasks 不应无界增长（同族需等上一实例完成；
    # 已完成任务会被裁剪，故用完成回调计数而非扫 tasks）
    done = []
    for i in range(5):
        ok, _, _ = queue.submit("quick", on_complete=lambda tid, code: done.append(tid))
        assert ok, f"第 {i + 1} 次提交被拒"
        deadline = time.time() + 30
        while time.time() < deadline and len(done) < i + 1:
            time.sleep(0.2)
        assert len(done) >= i + 1, f"第 {i + 1} 个任务完成回调未触发"
    assert len(queue.tasks) <= queue.max_history + 1, "历史任务应被裁剪"
