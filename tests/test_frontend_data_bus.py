"""data_bus 跨页联动机制测试。

覆盖三个层面：
1. 版本号语义：bump 递增、poll 门控（未变化 skip，变化后触发一次）
2. 任务队列集成：任务成功完成 → data_bus 版本号递增（跨页联动的触发源）
3. 页面构建冒烟：接入轮询的 6 个页面 render() 不报错（防 NameError/解包长度错）

背景（2026-08-31）：操作中心任务执行完后，其他页面数据停留在启动时的旧值，
根因是页面数据只在 render() 加载一次。修复方式为 data_bus 版本门控轮询，
本文件验证该机制的核心不变量。
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------- 版本号语义

def test_bump_increments_and_poll_detects_change():
    import data_bus

    before = data_bus.current()
    seen = data_bus.current()

    changed, cur = data_bus.poll(seen)
    assert not changed, "未 bump 前不应报告变化"
    assert cur == seen

    data_bus.bump("test")
    after = data_bus.current()
    assert after == before + 1, "bump 应使版本号恰好 +1"

    changed, cur = data_bus.poll(seen)
    assert changed, "bump 后 poll 应报告变化"
    assert cur == after, "poll 返回的版本号应等于当前版本"

    # 用新版本作为 seen 后不再报告变化
    changed, _ = data_bus.poll(cur)
    assert not changed


def test_poll_with_future_seen_self_heals():
    """seen 大于当前版本（如应用重启后浏览器残留旧状态）时：
    报告一次变化触发刷新，seen 被修正为当前版本后不再误报。"""
    import data_bus

    changed, cur = data_bus.poll(data_bus.current() + 100)
    assert changed, "残留的 seen 应触发一次刷新以自我修正"
    assert cur == data_bus.current()
    changed, _ = data_bus.poll(cur)
    assert not changed, "修正后不应再误报"


# ---------------------------------------------------------------- 任务队列集成

def test_completed_task_bumps_data_bus(monkeypatch, tmp_path):
    """任务成功完成 → data_bus.bump 被调用（前端各页联动的触发源）。"""
    import data_bus
    import api.task_api as task_api

    bumps: list[str] = []
    real_bump = data_bus.bump
    monkeypatch.setattr(data_bus, "bump", lambda reason="": bumps.append(reason) or real_bump(reason))

    before = data_bus.current()

    # 构造一个"成功完成"的任务，直接驱动 _start_task 内部回调路径：
    # 用假 Popen 模拟进程输出一行后退出 code=0
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["hello\n"])
    fake_proc.wait.return_value = 0
    fake_proc.poll.return_value = None
    fake_proc.pid = 12345
    monkeypatch.setattr(task_api.subprocess, "Popen", lambda *a, **kw: fake_proc)
    # 避免真 taskkill
    monkeypatch.setattr(task_api, "_kill_process_tree", lambda p: None)

    q = task_api.TaskQueue()
    tpl = task_api.TASKS["trading_calendar"]
    task = task_api.Task(
        task_id="trading_calendar",
        name=tpl["name"],
        script=tpl["script"],
        args=[],
        resource="data",
        family="trading_calendar",
    )
    q._start_task(task)
    # _start_task 启动守护线程，等待其跑完
    import time

    for _ in range(100):
        if task.status in (task_api.TaskStatus.COMPLETED, task_api.TaskStatus.FAILED):
            break
        time.sleep(0.05)

    assert task.status == task_api.TaskStatus.COMPLETED, f"任务应成功完成，实际 {task.status}"
    assert data_bus.current() == before + 1, "任务成功完成后 data_bus 版本应 +1"
    assert bumps == ["trading_calendar"], "bump 应以任务族 ID 为 reason"


def test_failed_task_does_not_bump(monkeypatch):
    """失败任务不应触发前端刷新（没有新数据）。"""
    import data_bus
    import api.task_api as task_api

    before = data_bus.current()

    fake_proc = MagicMock()
    fake_proc.stdout = iter(["boom\n"])
    fake_proc.wait.return_value = 1  # 非零退出码
    fake_proc.poll.return_value = None
    fake_proc.pid = 12345
    monkeypatch.setattr(task_api.subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(task_api, "_kill_process_tree", lambda p: None)

    q = task_api.TaskQueue()
    tpl = task_api.TASKS["trading_calendar"]
    task = task_api.Task(
        task_id="trading_calendar",
        name=tpl["name"],
        script=tpl["script"],
        args=[],
        resource="data",
        family="trading_calendar",
    )
    q._start_task(task)
    import time

    for _ in range(100):
        if task.status in (task_api.TaskStatus.COMPLETED, task_api.TaskStatus.FAILED):
            break
        time.sleep(0.05)

    assert task.status == task_api.TaskStatus.FAILED
    assert data_bus.current() == before, "失败任务不应 bump 版本"


# ---------------------------------------------------------------- 页面构建冒烟

@pytest.fixture
def ui_stubs(monkeypatch):
    """与 test_frontend_build 相同的 UI 替身，另需真实化 gr.State/gr.skip/gr.Timer。"""
    gradio = MagicMock()
    plotly_go = MagicMock()

    for name in ("Tab", "Accordion", "Row", "Column", "Group", "Blocks"):
        cm = getattr(gradio, name)
        cm.return_value.__enter__ = MagicMock(return_value=None)
        cm.return_value.__exit__ = MagicMock(return_value=False)

    # 轮询逻辑用到的真实语义对象（MagicMock 无法参与 gr.skip 判断）
    gradio.State = lambda v=None: v
    gradio.skip = MagicMock(return_value="SKIP")
    gradio.Timer = MagicMock()
    gradio.Update = MagicMock
    gradio.SelectData = MagicMock

    monkeypatch.setitem(sys.modules, "gradio", gradio)
    plotly_pkg = MagicMock()
    plotly_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "plotly", plotly_pkg)
    monkeypatch.setitem(sys.modules, "plotly.graph_objects", plotly_go)
    monkeypatch.setitem(sys.modules, "plotly.express", MagicMock())

    for name in [m for m in sys.modules if m.startswith(("frontend", "data_bus"))]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    return gradio


@pytest.mark.parametrize("page", ["home", "data_overview", "daily_signal", "backtest", "manual_trading", "strategy_monitor"])
def test_page_with_polling_builds(ui_stubs, page):
    """接入版本轮询的页面构建不报错（防 render 期 NameError / 组件引用错）。"""
    mod = importlib.import_module(f"frontend.pages.{page}")
    mod.render()
    assert ui_stubs.Tab.called
