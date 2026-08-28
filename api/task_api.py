"""任务 API - 任务执行相关

安全设计：
- 不使用 shell=True，防止命令注入
- 使用列表形式命令 + uv run 确保依赖正确
- 任务状态全局追踪
"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from typing import Callable, Optional


# 任务定义
TASKS = {
    "refresh": {
        "name": "数据刷新",
        "script": "scripts/update_data.py",
        "args": [],
        "icon": "🔄",
    },
    "backtest": {
        "name": "运行回测",
        "script": "scripts/run_backtest.py",
        "args": ["--strategy", "momentum_rotation"],
        "icon": "📈",
    },
    "signal": {
        "name": "生成信号",
        "script": "scripts/daily_signal.py",
        "args": [],
        "icon": "📋",
    },
    "ml_train": {
        "name": "ML训练",
        "script": "scripts/train_ml.py",
        "args": ["--start", "20240101"],
        "icon": "🤖",
    },
    "sweep": {
        "name": "参数扫描",
        "script": "scripts/sweep.py",
        "args": [],
        "icon": "🔍",
    },
    "factor_research": {
        "name": "因子研究",
        "script": "scripts/factor_research.py",
        "args": ["--sample", "monthly"],
        "icon": "🔬",
    },
}

# 全局任务状态追踪
_task_state = {
    "running": False,
    "last_task": None,
    "last_result": None,
    "last_start": None,
    "last_end": None,
    "process": None,
}

_state_lock = threading.Lock()


def _build_command(task: dict) -> list[str]:
    """构建安全的命令列表"""
    # 优先使用 uv run
    if os.system("uv --version >nul 2>&1") == 0 or os.system("uv --version >/dev/null 2>&1") == 0:
        return ["uv", "run", "python", "-u", task["script"]] + task.get("args", [])
    # 回退到虚拟环境 Python
    venv_python = os.path.join(".venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        return [venv_python, "-u", task["script"]] + task.get("args", [])
    import sys
    return [sys.executable, "-u", task["script"]] + task.get("args", [])


def run_task(
    task_name: str,
    on_output: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[int], None]] = None,
) -> bool:
    """运行任务（安全版本，无 shell=True）"""
    if task_name not in TASKS:
        return False

    with _state_lock:
        if _task_state["running"]:
            return False  # 已有任务在运行

    task = TASKS[task_name]
    cmd = _build_command(task)

    with _state_lock:
        _task_state["running"] = True
        _task_state["last_task"] = task_name
        _task_state["last_start"] = datetime.now()
        _task_state["last_result"] = None

    def _run():
        try:
            on_output_safe = on_output or (lambda x: None)
            on_output_safe(f"$ {' '.join(cmd)}")

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=".",
                env=env,
                bufsize=1,
            )

            with _state_lock:
                _task_state["process"] = process

            for line in process.stdout:
                on_output_safe(line.rstrip())

            returncode = process.wait()

            with _state_lock:
                _task_state["running"] = False
                _task_state["last_result"] = returncode
                _task_state["last_end"] = datetime.now()
                _task_state["process"] = None

            if on_complete:
                on_complete(returncode)

        except Exception as e:
            with _state_lock:
                _task_state["running"] = False
                _task_state["last_result"] = -1
                _task_state["last_end"] = datetime.now()
                _task_state["process"] = None
            if on_output:
                on_output(f"错误: {str(e)}")
            if on_complete:
                on_complete(-1)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return True


def get_task_status() -> dict:
    """获取任务状态"""
    with _state_lock:
        result = {
            "running": _task_state["running"],
            "last_task": _task_state["last_task"],
            "last_result": _task_state["last_result"],
        }
        if _task_state["last_start"]:
            result["last_start"] = _task_state["last_start"].strftime("%H:%M:%S")
        if _task_state["last_end"]:
            result["last_end"] = _task_state["last_end"].strftime("%H:%M:%S")
        return result


def cancel_task() -> bool:
    """取消当前运行的任务"""
    with _state_lock:
        if _task_state["process"]:
            _task_state["process"].terminate()
            return True
    return False
