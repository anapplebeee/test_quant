"""任务 API - 队列化并发任务执行系统

功能：
- 任务队列：多个任务可排队等待执行
- 并发执行：无冲突任务可并行运行
- 冲突检测：共享资源的任务自动排队
- 进度追踪：实时进度和状态查询
"""
from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"       # 排队中
    RUNNING = "running"       # 运行中
    COMPLETED = "completed"   # 成功
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 已取消


# 任务定义
TASKS = {
    "refresh": {
        "name": "数据刷新",
        "script": "scripts/update_data.py",
        "args": [],
        "icon": "🔄",
        "resource": "data",          # 占用资源：数据文件
    },
    "backtest": {
        "name": "运行回测",
        "script": "scripts/run_backtest.py",
        "args": ["--strategy", "momentum_rotation"],
        "icon": "📈",
        "resource": "compute",       # 占用资源：CPU计算
    },
    "signal": {
        "name": "生成信号",
        "script": "scripts/daily_signal.py",
        "args": [],
        "icon": "📋",
        "resource": "data",          # 读取数据+写入报告
    },
    "ml_train": {
        "name": "ML训练",
        "script": "scripts/train_ml.py",
        "args": ["--start", "20240101"],
        "icon": "🤖",
        "resource": "compute",
    },
    "sweep": {
        "name": "参数扫描",
        "script": "scripts/sweep.py",
        "args": [],
        "icon": "🔍",
        "resource": "compute",
    },
    "factor_research": {
        "name": "因子研究",
        "script": "scripts/factor_research.py",
        "args": ["--sample", "monthly"],
        "icon": "🔬",
        "resource": "data",
    },
}

# 资源冲突矩阵：同一资源同一时间只能有 1 个任务
# compute 类任务可以最多并行 2 个（多核CPU）
RESOURCE_LIMITS = {
    "data": 1,      # 数据资源串行
    "compute": 2,   # 计算资源可并行2个
}


@dataclass
class Task:
    """单个任务实例"""
    task_id: str
    name: str
    script: str
    args: list
    resource: str
    status: TaskStatus = TaskStatus.PENDING
    output_lines: list = field(default_factory=list)
    returncode: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    process: Optional[subprocess.Popen] = None

    @property
    def progress_hint(self) -> str:
        """进度提示（基于日志行数估算）"""
        if self.status == TaskStatus.PENDING:
            return "排队中..."
        if self.status == TaskStatus.RUNNING:
            n = len(self.output_lines)
            if n == 0:
                return "启动中..."
            return f"运行中 ({n} 行输出)"
        if self.status == TaskStatus.COMPLETED:
            return f"✅ 完成 ({self.ended_at.strftime('%H:%M:%S') if self.ended_at else ''})"
        if self.status == TaskStatus.FAILED:
            return f"❌ 失败 (code={self.returncode})"
        return "已取消"

    def to_dict(self) -> dict:
        return {
            "id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "progress": self.progress_hint,
            "lines": len(self.output_lines),
            "returncode": self.returncode,
        }


class TaskQueue:
    """任务队列管理器

    - 同资源任务串行排队
    - 不同资源任务可并行
    - compute 资源最多2个并行
    """

    def __init__(self, max_history: int = 50):
        self.max_history = max_history
        self.tasks: dict[str, Task] = {}       # 全部任务记录
        self.queue_order: deque[str] = deque()  # FIFO 顺序
        self._lock = threading.Lock()
        self._dispatch_thread: Optional[threading.Thread] = None
        self._shutdown = False

    # ---------- 公共接口 ----------

    def submit(self, task_id: str, on_output: Optional[Callable] = None,
               on_complete: Optional[Callable] = None) -> tuple[bool, str]:
        """提交任务到队列

        Returns:
            (是否成功, 消息)
        """
        if task_id not in TASKS:
            return False, f"未知任务: {task_id}"

        with self._lock:
            # 检查是否有相同任务在排队/运行
            for t in self.tasks.values():
                if t.task_id == task_id and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                    return False, f"'{TASKS[task_id]['name']}' 已在队列中，请等待完成"

            # 创建任务
            tpl = TASKS[task_id]
            # 用序号区分同名的多次执行
            seq = sum(1 for t in self.tasks.values() if t.task_id == task_id) + 1
            instance_id = f"{task_id}#{seq}" if seq > 1 else task_id

            task = Task(
                task_id=instance_id,
                name=tpl["name"],
                script=tpl["script"],
                args=list(tpl.get("args", [])),
                resource=tpl.get("resource", "compute"),
            )
            self.tasks[instance_id] = task
            self.queue_order.append(instance_id)

        # 启动调度器（如果未运行）
        self._ensure_dispatcher(on_output, on_complete)

        # 触发一次调度
        self._dispatch(on_output, on_complete)
        return True, f"已提交: {task.name}"

    def get_status_summary(self) -> str:
        """获取所有任务状态摘要"""
        with self._lock:
            if not self.tasks:
                return "暂无任务"

            running = [t for t in self.tasks.values() if t.status == TaskStatus.RUNNING]
            pending = [t for t in self.tasks.values() if t.status == TaskStatus.PENDING]
            finished = [t for t in self.tasks.values()
                        if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)][-5:]

            lines = []
            if running:
                lines.append("▶ 运行中:")
                for t in running:
                    lines.append(f"  {t.name} - {t.progress_hint}")
            if pending:
                lines.append(f"⏳ 排队中 ({len(pending)}):")
                for t in pending:
                    lines.append(f"  {t.name}")
            if finished:
                lines.append("最近完成:")
                for t in finished:
                    icon = "✅" if t.status == TaskStatus.COMPLETED else "❌"
                    lines.append(f"  {icon} {t.name} - {t.progress_hint}")

            return "\n".join(lines)

    def get_output(self, task_id: str, tail: int = 60) -> str:
        """获取任务输出"""
        with self._lock:
            task = self.tasks.get(task_id)
        if not task:
            return "任务不存在"
        return "\n".join(task.output_lines[-tail:]) or "等待输出..."

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """取消任务"""
        with self._lock:
            task = self.tasks.get(task_id)
        if not task:
            return False, "任务不存在"
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            return True, "已取消排队任务"
        if task.status == TaskStatus.RUNNING and task.process:
            task.process.terminate()
            task.status = TaskStatus.CANCELLED
            return True, "已终止运行中任务"
        return False, f"无法取消 (状态={task.status.value})"

    # ---------- 内部方法 ----------

    def _build_command(self, task: Task) -> list[str]:
        """构建安全命令"""
        if os.system("uv --version >nul 2>&1") == 0 or os.system("uv --version >/dev/null 2>&1") == 0:
            return ["uv", "run", "python", "-u", task.script] + task.args
        venv_python = os.path.join(".venv", "Scripts", "python.exe")
        if os.path.exists(venv_python):
            return [venv_python, "-u", task.script] + task.args
        import sys
        return [sys.executable, "-u", task.script] + task.args

    def _ensure_dispatcher(self, on_output=None, on_complete=None):
        """确保调度线程运行"""
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            return
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, args=(on_output, on_complete), daemon=True)
        self._dispatch_thread.start()

    def _dispatch_loop(self, on_output=None, on_complete=None):
        """后台调度循环"""
        import time
        while not self._shutdown:
            self._dispatch(on_output, on_complete)
            time.sleep(0.5)

    def _dispatch(self, on_output=None, on_complete=None):
        """调度：检查资源，启动可运行任务"""
        with self._lock:
            # 统计各资源当前运行数
            resource_running: dict[str, int] = {}
            for t in self.tasks.values():
                if t.status == TaskStatus.RUNNING:
                    resource_running[t.resource] = resource_running.get(t.resource, 0) + 1

            # 遍历队列，找到可以启动的任务
            for instance_id in list(self.queue_order):
                task = self.tasks.get(instance_id)
                if not task or task.status != TaskStatus.PENDING:
                    self.queue_order.remove(instance_id)
                    continue

                limit = RESOURCE_LIMITS.get(task.resource, 1)
                current = resource_running.get(task.resource, 0)
                if current >= limit:
                    continue  # 资源满，跳过（保留排队）

                # 启动任务
                self._start_task(task, on_output, on_complete)
                resource_running[task.resource] = current + 1

    def _start_task(self, task: Task, on_output=None, on_complete=None):
        """启动单个任务"""
        cmd = self._build_command(task)
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        task.output_lines.append(f"$ {' '.join(cmd)}")

        def _run():
            try:
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
                task.process = process

                for line in process.stdout:
                    task.output_lines.append(line.rstrip())
                    if on_output:
                        try:
                            on_output(task.task_id, line.rstrip())
                        except Exception:
                            pass

                returncode = process.wait()
                task.returncode = returncode
                task.ended_at = datetime.now()
                task.status = TaskStatus.COMPLETED if returncode == 0 else TaskStatus.FAILED
                task.process = None

                if on_complete:
                    try:
                        on_complete(task.task_id, returncode)
                    except Exception:
                        pass

            except Exception as e:
                task.output_lines.append(f"错误: {e}")
                task.returncode = -1
                task.ended_at = datetime.now()
                task.status = TaskStatus.FAILED
                if on_complete:
                    try:
                        on_complete(task.task_id, -1)
                    except Exception:
                        pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


# 全局任务队列单例
task_queue = TaskQueue()


# 兼容旧接口
def run_task(task_name: str, on_output=None, on_complete=None) -> tuple[bool, str]:
    """提交任务（兼容旧接口）"""
    return task_queue.submit(task_name, on_output, on_complete)


def get_task_status() -> str:
    """获取任务状态摘要"""
    return task_queue.get_status_summary()
