"""任务执行器 - 共享模块"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Callable, Optional


# 可执行任务定义
TASKS = {
    "update_data": {
        "name": "数据刷新",
        "script": "scripts/update_data.py",
        "description": "更新A股日线数据（增量）",
        "icon": "🔄",
    },
    "full_market": {
        "name": "全量更新",
        "script": "scripts/_launch_fullmarket.py",
        "description": "全量刷新所有股票数据",
        "icon": "📥",
    },
    "train_ml": {
        "name": "ML训练",
        "script": "scripts/train_ml.py",
        "description": "训练Alpha158+LGBM模型",
        "icon": "🤖",
    },
    "daily_signal": {
        "name": "生成信号",
        "script": "scripts/daily_signal.py",
        "description": "生成今日交易信号",
        "icon": "📋",
    },
    "run_backtest": {
        "name": "运行回测",
        "script": "scripts/run_backtest.py",
        "description": "执行策略回测",
        "icon": "📈",
    },
    "factor_research": {
        "name": "因子研究",
        "script": "scripts/factor_research.py",
        "description": "计算因子IC/ICIR",
        "icon": "🔬",
    },
    "export_qlib": {
        "name": "导出Qlib",
        "script": "scripts/export_to_qlib.py",
        "description": "导出数据到Qlib格式",
        "icon": "📦",
    },
}


def run_task(
    task_id: str,
    on_output: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[int], None]] = None,
    extra_args: list[str] | None = None,
) -> threading.Thread | None:
    """在后台线程中执行任务"""
    if task_id not in TASKS:
        return None

    task = TASKS[task_id]
    script = task["script"]

    def _run():
        try:
            cmd = [sys.executable, script] + (extra_args or [])
            # 使用项目虚拟环境的 Python
            env = __import__("os").environ.copy()

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

            # 实时读取输出
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line and on_output:
                    on_output(line.rstrip())

            returncode = process.poll()
            if on_complete:
                on_complete(returncode)

        except Exception as e:
            if on_output:
                on_output(f"执行出错: {e}")
            if on_complete:
                on_complete(-1)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
