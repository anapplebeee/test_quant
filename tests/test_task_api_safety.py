"""任务 API 参数注入防护测试。

task_api 用 `subprocess` 把 UI 参数拼进命令行。未经校验的输入等于
让前端页面可以指定任意命令行参数（例如 `--save-dir` 指向任意路径）。
"""
from __future__ import annotations

import sys

import pytest

from api.task_api import ALLOWED_ARGS, TASKS, validate_extra_args


@pytest.mark.parametrize("task_id", ["backtest", "sweep", "signal", "refresh", "ml_train"])
def test_every_task_has_an_arg_policy(task_id):
    """新增任务时必须显式声明参数白名单，缺省即拒绝（fail-closed）。"""
    assert task_id in ALLOWED_ARGS


def test_every_registered_task_is_fail_closed():
    assert set(TASKS) <= set(ALLOWED_ARGS)


def test_accepts_whitelisted_args():
    ok, err = validate_extra_args("backtest", ["--strategy", "lowvol_indz"])
    assert ok, err


def test_accepts_top_k_and_rebalance():
    ok, err = validate_extra_args(
        "backtest",
        [
            "--top-k",
            "20",
            "--rebalance-days",
            "45",
            "--rev-weight",
            "0.3",
            "--cost-multiplier",
            "2",
        ],
    )
    assert ok, err


def test_accepts_path_momentum_parameters():
    ok, err = validate_extra_args(
        "backtest",
        [
            "--strategy", "momentum_path",
            "--momentum-mode", "smooth",
            "--lookback-days", "120",
            "--momentum-skip-days", "20",
            "--limit-up-threshold", "0.095",
            "--regime-mode", "score",
            "--timing-levels", "3",
        ],
    )
    assert ok, err


def test_accepts_boolean_flags():
    ok, err = validate_extra_args("backtest", ["--no-regime"])
    assert ok, err


def test_accepts_frontend_signal_and_refresh_parameters():
    ok, err = validate_extra_args(
        "signal",
        ["--strategy", "lowvol_indz", "--trade-date", "2026-09-01", "--no-push"],
    )
    assert ok, err
    ok, err = validate_extra_args(
        "refresh",
        [
            "--universe", "index",
            "--index", "000300",
            "--start", "20190101",
            "--workers", "4",
            "--full-refresh",
        ],
    )
    assert ok, err


def test_refresh_workers_are_bounded():
    ok, _ = validate_extra_args("refresh", ["--workers", "16"])
    assert ok
    ok, _ = validate_extra_args("refresh", ["--workers", "32"])
    assert ok, "32 是并发上限，应允许"
    ok, _ = validate_extra_args("refresh", ["--workers", "33"])
    assert not ok, "超过 32 必须拒绝"


def test_rejects_unknown_flag():
    """注入未声明的参数必须被拒。"""
    ok, _ = validate_extra_args("backtest", ["--save-dir", "/tmp/evil"])
    assert not ok


def test_rejects_path_traversal_in_value():
    ok, _ = validate_extra_args("backtest", ["--strategy", "../../etc/passwd"])
    assert not ok


def test_rejects_shell_metacharacters():
    ok, _ = validate_extra_args("backtest", ["--strategy", "x; rm -rf /"])
    assert not ok
    ok, _ = validate_extra_args("backtest", ["--strategy", "a && calc"])
    assert not ok


def test_rejects_non_numeric_where_number_expected():
    ok, _ = validate_extra_args("backtest", ["--top-k", "abc"])
    assert not ok


def test_rejects_missing_value():
    ok, _ = validate_extra_args("backtest", ["--strategy"])
    assert not ok


def test_rejects_value_only():
    ok, _ = validate_extra_args("backtest", ["lowvol_indz"])
    assert not ok


def test_rejects_bad_enum_value():
    ok, _ = validate_extra_args("factor_research", ["--sample", "hourly"])
    assert not ok
    ok, _ = validate_extra_args("factor_research", ["--sample", "monthly"])
    assert ok


def test_rejects_args_for_task_that_allows_none():
    ok, _ = validate_extra_args("refresh", ["--anything", "1"])
    assert not ok


def test_rejects_unregistered_task():
    ok, _ = validate_extra_args("totally_made_up", ["--x", "1"])
    assert not ok


def test_empty_args_always_ok():
    for task_id in ALLOWED_ARGS:
        ok, err = validate_extra_args(task_id, None)
        assert ok, f"{task_id}: {err}"


def test_submit_rejects_injected_args():
    """端到端：submit 必须挡住非法参数，不能只是 submit 内部忽略。"""
    from api.task_api import TaskQueue

    q = TaskQueue()
    ok, msg, _instance = q.submit("backtest", extra_args=["--save-dir", "/tmp/evil"])
    assert not ok
    assert "白名单" in msg
    # 被拒后不应留下任务记录
    assert not any(t.family == "backtest" for t in q.tasks.values())


def test_task_command_prefers_current_python_over_global_uv(monkeypatch):
    """后台任务必须与前端服务共用解释器，避免依赖和 uv 缓存漂移。"""
    import api.task_api as task_api

    monkeypatch.setattr(task_api.shutil, "which", lambda name: "C:/tools/uv.exe")
    task = task_api.Task(
        task_id="refresh",
        name="刷新",
        script="scripts/update_data.py",
        args=[],
        resource="data",
    )

    command = task_api.TaskQueue()._build_command(task)

    assert command[0] == sys.executable
    assert command[1:3] == ["-u", "scripts/update_data.py"]
