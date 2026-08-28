"""任务 API - 任务执行相关"""
from __future__ import annotations

import subprocess
import threading
from typing import Callable, Optional


# 任务定义
TASKS = {
    "refresh": {
        "name": "数据刷新",
        "command": "python scripts/update_data.py",
        "icon": "🔄",
    },
    "backtest": {
        "name": "运行回测",
        "command": "python scripts/run_backtest.py --strategy momentum_rotation",
        "icon": "📈",
    },
    "signal": {
        "name": "生成信号",
        "command": "python scripts/daily_signal.py",
        "icon": "📋",
    },
    "ml_train": {
        "name": "ML训练",
        "command": "python scripts/train_ml.py --start 20240101",
        "icon": "🤖",
    },
    "sweep": {
        "name": "参数扫描",
        "command": "python scripts/sweep.py",
        "icon": "🔍",
    },
}


def run_task(
    task_name: str,
    on_output: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[int], None]] = None,
) -> bool:
    """
    运行任务
    
    Args:
        task_name: 任务名称
        on_output: 输出回调
        on_complete: 完成回调
    
    Returns:
        是否成功启动
    """
    if task_name not in TASKS:
        return False
    
    task = TASKS[task_name]
    command = task["command"]
    
    def _run():
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            
            for line in process.stdout:
                if on_output:
                    on_output(line.strip())
            
            process.wait()
            
            if on_complete:
                on_complete(process.returncode)
        except Exception as e:
            if on_output:
                on_output(f"错误: {str(e)}")
            if on_complete:
                on_complete(-1)
    
    # 在后台线程运行
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    
    return True


def get_task_status() -> dict:
    """获取任务状态"""
    # TODO: 实现任务状态检查
    return {
        "running": False,
        "last_task": None,
        "last_result": None,
    }
