"""任务 API - 队列化并发任务执行系统

功能：
- 任务队列：多个任务可排队等待执行
- 并发执行：无冲突任务可并行运行
- 冲突检测：共享资源的任务自动排队
- 进度追踪：实时进度和状态查询
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class TaskStatus(StrEnum):
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
"resource": "data",
"timeout": 14400,
        "outputs": {
            "更新状态": "data/meta/last_data_update.json",
            "日线数据": "data/daily/year=*/*.parquet",
            "股票池快照": "data/universe/*.parquet",
        },
        "result_tab": "🗃️ 数据总览",
    },
    "backtest": {
"name": "运行回测",
"script": "scripts/run_backtest.py",
"args": [],                      # 策略由 UI 选择，动态传入
"icon": "📈",
"resource": "compute",
"timeout": 3600,
        "outputs": {
            "回测摘要": "reports/summary_*.json",
            "净值曲线": "reports/sweep_equity_*.csv",
            "交易记录": "reports/trades_*.csv",
        },
        "result_tab": "📈 回测中心",
        "has_strategy_select": True,
    },
    "signal": {
"name": "生成信号",
"script": "scripts/daily_signal.py",
"args": [],
"icon": "📋",
"resource": "data",
"timeout": 1800,
        "outputs": {
            "信号报告": "reports/signal_*.md",
            "ML预测分数": "data/scores/preds.csv",
        },
        "result_tab": "📋 每日信号",
    },
    "ml_train": {
"name": "ML训练",
"script": "scripts/train_ml.py",
"args": ["--start", "20240101"],
"icon": "🤖",
"resource": "compute",
"timeout": 7200,
        "outputs": {
            "ML预测分数": "data/scores/preds.csv",
            "模型元数据": "data/scores/meta.json",
        },
        "result_tab": "📋 每日信号",
    },
    "sweep": {
"name": "参数扫描",
"script": "scripts/sweep.py",
"args": [],
"icon": "🔍",
"resource": "compute",
"timeout": 7200,
        "outputs": {
            "扫描结果": "reports/sweep_*.csv",
            "扫描净值": "reports/sweep_equity_*.csv",
        },
        "result_tab": "📈 回测中心",
    },
    "factor_research": {
        "name": "因子研究",
        "script": "scripts/factor_audit.py",
        "args": ["--sample", "monthly"],
        "icon": "🔬",
        "resource": "data",
        "timeout": 7200,
        "outputs": {
            "因子审计汇总": "reports/factor_audit_summary.csv",
            "滚动 IC": "reports/factor_audit_ic_history.csv",
            "因子相关性": "reports/factor_audit_correlation.csv",
            "制品目录": "artifacts/factor_audit_*/manifest.json",
        },
        "result_tab": "🔬 因子研究",
    },
    "walk_forward": {
        "name": "Walk-Forward 验证",
        "script": "scripts/walk_forward.py",
        "args": [],
        "icon": "🔁",
        "resource": "compute",
        "timeout": 7200,
        "outputs": {
            "逐折明细": "reports/wfa_*.csv",
            "制品目录": "artifacts/wfa_*/manifest.json",
        },
        "result_tab": "📈 回测中心",
        "has_strategy_select": True,
    },
    "migrate_store": {
        "name": "存储分区迁移",
        "script": "scripts/migrate_partition_store.py",
        "args": [],
        "icon": "🗄️",
        "resource": "data",
        "timeout": 3600,
        "outputs": {},
        "result_tab": "🗃️ 数据总览",
    },
    "data_quality": {
        "name": "数据质量扫描",
        "script": "scripts/data_quality_scan.py",
        "args": [],
        "icon": "🧪",
        "resource": "data",
        "timeout": 1800,
        "outputs": {
            "问题明细": "reports/data_quality_scan.csv",
            "制品目录": "artifacts/data_quality_*/manifest.json",
        },
        "result_tab": "🗃️ 数据总览",
    },
    "universe_history": {
        "name": "构建 PIT 股票池历史",
        "script": "scripts/build_universe_history.py",
        "args": [],
        "icon": "🧭",
        "resource": "data",
        "timeout": 3600,
        "outputs": {"股票池历史": "data/universe_history/*.parquet"},
        "result_tab": "🗃️ 数据总览",
    },
    "industries": {
        "name": "更新行业映射",
        "script": "scripts/fetch_industries.py",
        "args": [],
        "icon": "🏭",
        "resource": "data",
        "timeout": 1800,
        "outputs": {"行业映射": "data/meta/industries*.csv"},
        "result_tab": "🌿 因子生态",
    },
    "financial_factors": {
        "name": "更新财务因子",
        "script": "scripts/fetch_financial_factors.py",
        "args": [],
        "icon": "💹",
        "resource": "data",
        "timeout": 7200,
        "outputs": {"财务因子": "data/factors/*.parquet"},
        "result_tab": "🔬 因子研究",
    },
    "trading_calendar": {
        "name": "更新交易日历",
        "script": "scripts/update_trading_calendar.py",
        "args": [],
        "icon": "📅",
        "resource": "data",
        "timeout": 1800,
        "outputs": {"交易日历": "data/meta/trading_calendar.csv"},
        "result_tab": "🧰 操作中心",
    },
    "update_indices": {
        "name": "更新常用指数",
        "script": "scripts/update_indices.py",
        "args": [],
        "icon": "📈",
        "resource": "data",
        "timeout": 3600,
        "outputs": {"指数日线": "data/index/year=*/IDX*.parquet"},
        "result_tab": "🗃️ 数据总览",
    },
}

# 资源冲突矩阵：同一资源同一时间只能有 1 个任务
# compute 类任务可以最多并行 2 个（多核CPU）
RESOURCE_LIMITS = {
    "data": 1,      # 数据资源串行
    "compute": 2,   # 计算资源可并行2个
}

# 每个任务允许从 UI 传入的参数白名单：选项 -> 取值校验正则
# task_api 用 subprocess 拼命令行，未校验的 UI 输入 = 任意参数注入
# （例如注入 --save-dir 把产物写到任意路径）。
ALLOWED_ARGS: dict[str, dict[str, str]] = {
    "backtest": {
        "--strategy": r"^[A-Za-z0-9_]+$",
        "--start": r"^\d{4}-\d{2}-\d{2}$",
        "--end": r"^\d{4}-\d{2}-\d{2}$",
        "--research-mode": r"^(formal|exploratory)$",
        "--universe-index": r"^\d{6}$",
        "--index": r"^\d{6}$",
        "--rebalance-days": r"^\d{1,3}$",
        "--top-k": r"^\d{1,3}$",
        "--rev-weight": r"^(0(\.\d+)?|1(\.0+)?)$",
        "--regime-mode": r"^(ma|score)$",
        "--timing-levels": r"^\d{1,2}$",
        "--momentum-mode": r"^(simple|rank|smooth|remove_limit_up)$",
        "--lookback-days": r"^\d{1,4}$",
        "--momentum-skip-days": r"^\d{1,3}$",
        "--limit-up-threshold": r"^(0\.\d+|1\.0+)$",
        "--cost-multiplier": r"^(0(\.\d+)?|[1-9](\.\d+)?|10(\.0+)?)$",
        "--no-regime": None,   # 开关型，不带值
        "--no-risk": None,
    },
    "sweep": {
        "--strategy": r"^[A-Za-z0-9_]+$",
        "--start": r"^\d{4}-\d{2}-\d{2}$",
        "--end": r"^\d{4}-\d{2}-\d{2}$",
        "--combo": r"^[A-Za-z0-9_=.,\-]+$",
    },
    "walk_forward": {
        "--strategy": r"^[A-Za-z0-9_]+$",
        "--start": r"^\d{4}-\d{2}-\d{2}$",
        "--end": r"^\d{4}-\d{2}-\d{2}$",
        "--train": r"^\d{1,4}$",
        "--test": r"^\d{1,4}$",
        "--step": r"^\d{1,4}$",
        "--embargo": r"^\d{1,3}$",
        "--metric": r"^(sharpe|cagr|calmar|total_return|bench_excess_cagr)$",
        "--min-trades": r"^\d{1,6}$",
        "--warmup": r"^\d{1,4}$",
        "--grid": r"^[A-Za-z0-9_=.,\-]+$",
        "--account-mode": r"^(continuous|independent)$",
        "--anchored": None,
        "--no-risk": None,
    },
    "migrate_store": {"--root": r"^[A-Za-z0-9_.:/\\\-]+$", "--dry-run": None},
    "signal": {
        "--strategy": r"^[A-Za-z0-9_]+$",
        "--trade-date": r"^\d{4}-\d{2}-\d{2}$",
        "--no-push": None,
    },
    "refresh": {
        "--universe": r"^(index|mainboard|all)$",
        "--index": r"^\d{6}$",
        "--start": r"^\d{8}$",
        "--max": r"^\d{1,5}$",
        "--workers": r"^(?:[1-9]|1\d|2\d|3[0-2])$",
        "--keep-st": None,
        # --full 是 --full-refresh 的兼容别名（update_data 里 dest 相同）
        "--full-refresh": None,
        "--full": None,
    },
    "ml_train": {"--start": r"^\d{8}$"},
    "factor_research": {
        "--sample": r"^(weekly|monthly)$",
        "--horizon": r"^\d{1,2}$",
        "--start": r"^\d{4}-\d{2}-\d{2}$",
        "--end": r"^\d{4}-\d{2}-\d{2}$",
        "--min-amount": r"^\d+(?:\.\d+)?$",
        "--min-cross-section": r"^\d{1,5}$",
    },
    "data_quality": {"--jumps": r"^(0(\.\d+)?|1(\.0+)?)$"},
    "universe_history": {"--index": r"^\d{6}$", "--describe-only": None},
    "industries": {"--refresh": None},
    "financial_factors": {},
    "trading_calendar": {},
    "update_indices": {"--codes": r"^[0-9,]+$"},
}

# 开关型参数（不带值）
_FLAG_ONLY = {
    "--no-regime",
    "--no-risk",
    "--anchored",
    "--dry-run",
    "--no-push",
    "--keep-st",
    "--full-refresh",
    "--describe-only",
    "--refresh",
    "--full",
}


def validate_extra_args(task_id: str, extra_args: list | None) -> tuple[bool, str]:
    """校验 UI 传入的命令行参数。

    Returns
    -------
    (是否通过, 错误信息)
    """
    if not extra_args:
        return True, ""
    allowed = ALLOWED_ARGS.get(task_id)
    if allowed is None:
        return False, f"任务 '{task_id}' 不接受外部参数"

    args = [str(a) for a in extra_args]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg not in allowed:
            return False, f"参数 '{arg}' 不在任务 '{task_id}' 的白名单内"
        pattern = allowed[arg]
        if arg in _FLAG_ONLY or pattern is None:
            i += 1
            continue
        if i + 1 >= len(args):
            return False, f"参数 '{arg}' 缺少取值"
        import re

        value = args[i + 1]
        if not re.fullmatch(pattern, value):
            return False, f"参数 '{arg}' 取值非法: {value!r}"
        i += 2
    return True, ""


@dataclass
class Task:
    """单个任务实例"""
    task_id: str
    name: str
    script: str
    args: list
    resource: str
    family: str = ""                 # 任务类型（如 'backtest'），实例 id 可能是 'backtest#2'
    status: TaskStatus = TaskStatus.PENDING
    output_lines: list = field(default_factory=list)
    returncode: int | None = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    process: subprocess.Popen | None = None
    on_output: Callable | None = None    # 回调随任务实例注册，不再依赖首次 submit（修复排队任务收不到事件）
    on_complete: Callable | None = None
    cancel_requested: bool = False

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
        self._dispatch_thread: threading.Thread | None = None
        self._shutdown = False

    # ---------- 公共接口 ----------

    def submit(self, task_id: str, on_output: Callable | None = None,
               on_complete: Callable | None = None,
               extra_args: list | None = None) -> tuple[bool, str, str]:
        """提交任务到队列

        Args:
            extra_args: 附加命令行参数（如 ["--strategy", "lowvol_composite"]）

        Returns:
            (是否成功, 消息, 实例ID)  实例ID 可用于 get_output/cancel/完成事件匹配
        """
        if task_id not in TASKS:
            return False, f"未知任务: {task_id}", ""

        # UI 传入的参数直接进命令行，必须白名单校验
        ok, err = validate_extra_args(task_id, extra_args)
        if not ok:
            return False, err, ""

        with self._lock:
            # 检查是否有相同任务在排队/运行
            for t in self.tasks.values():
                if t.family == task_id and t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                    return False, f"'{TASKS[task_id]['name']}' 已在队列中，请等待完成", ""

            # 创建任务
            tpl = TASKS[task_id]
            # 用序号区分同名的多次执行
            seq = sum(1 for t in self.tasks.values() if t.family == task_id) + 1
            instance_id = f"{task_id}#{seq}" if seq > 1 else task_id

            # 合并默认参数和动态参数
            final_args = list(tpl.get("args", [])) + list(extra_args or [])

            task = Task(
                task_id=instance_id,
                name=tpl["name"],
                script=tpl["script"],
                args=final_args,
                resource=tpl.get("resource", "compute"),
                family=task_id,
                on_output=on_output,
                on_complete=on_complete,
            )
            self.tasks[instance_id] = task
            self.queue_order.append(instance_id)
            self._trim_history_locked()

        # 启动调度器（如果未运行；带锁单例，修复可重复启动调度线程的问题）
        self._ensure_dispatcher()

        # 触发一次调度
        self._dispatch()
        return True, f"已提交: {task.name}", instance_id

    def _trim_history_locked(self) -> None:
        """历史任务数量控制（防 tasks/output_lines 无界增长），须持有 _lock 调用"""
        if len(self.tasks) <= self.max_history:
            return
        finished = [t for t in self.tasks.values()
                    if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)]
        finished.sort(key=lambda t: t.created_at)
        for t in finished[: len(self.tasks) - self.max_history]:
            self.tasks.pop(t.task_id, None)
            with suppress(ValueError):
                self.queue_order.remove(t.task_id)

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
        """获取任务输出（支持实例 id 或任务族 id，后者取最新实例）"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                candidates = [t for t in self.tasks.values()
                              if t.family == task_id or t.task_id.startswith(task_id)]
                task = max(candidates, key=lambda t: t.created_at, default=None)
        if not task:
            return "任务不存在"
        return "\n".join(task.output_lines[-tail:]) or "等待输出..."

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """取消任务（运行中则杀整个进程树）"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False, "任务不存在"
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                return True, "已取消排队任务"
            if task.status == TaskStatus.RUNNING and task.process:
                task.cancel_requested = True
                _kill_process_tree(task.process)
                task.status = TaskStatus.CANCELLED
                return True, "已终止运行中任务（含子进程）"
            return False, f"无法取消 (状态={task.status.value})"

    # ---------- 内部方法 ----------

    def _build_command(self, task: Task) -> list[str]:
        """构建安全命令，并与当前应用保持同一 Python 运行时。"""
        import sys

        # 服务已成功启动说明当前解释器具备应用依赖。优先复用它可避免 PATH
        # 上的 uv/system Python 生成不同版本的子进程，也不依赖全局 uv 缓存。
        if sys.executable and os.path.exists(sys.executable):
            return [sys.executable, "-u", task.script, *task.args]
        venv_python = os.path.join(".venv", "Scripts", "python.exe")
        if os.path.exists(venv_python):
            return [venv_python, "-u", task.script, *task.args]
        if shutil.which("uv"):
            return ["uv", "run", "python", "-u", task.script, *task.args]
        return ["python", "-u", task.script, *task.args]

    def _ensure_dispatcher(self):
        """确保调度线程运行（带锁单例，回调从任务实例读取）"""
        with self._lock:
            if self._dispatch_thread and self._dispatch_thread.is_alive():
                return
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop, daemon=True)
            self._dispatch_thread.start()

    def _dispatch_loop(self):
        """后台调度循环"""
        import time
        while not self._shutdown:
            self._dispatch()
            time.sleep(0.5)

    def _dispatch(self):
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
                self._start_task(task)
                resource_running[task.resource] = current + 1

    def _start_task(self, task: Task):
        """启动单个任务（回调从任务实例读取，每个提交者都能收到自己的事件）"""
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

                # 超时看门狗：stdout 迭代会阻塞，无法在主路径轮询超时，
                # 用独立 Timer 线程到点强杀进程树
                timeout_s = int(TASKS.get(task.family, {}).get("timeout", 3600))
                timed_out: list[bool] = []

                def _on_timeout() -> None:
                    if task.process and task.process.poll() is None:
                        timed_out.append(True)
                        task.output_lines.append(
                            f"\n⏱️ 任务超过 {timeout_s}s 未完成，已强制终止（可在 TASKS 中调 timeout）")
                        _kill_process_tree(task.process)

                watchdog = threading.Timer(timeout_s, _on_timeout)
                watchdog.daemon = True
                watchdog.start()

                try:
                    for line in process.stdout:
                        task.output_lines.append(line.rstrip())
                        if len(task.output_lines) > 2000:  # 防单任务输出无界增长
                            del task.output_lines[:1000]
                        if task.on_output:
                            with suppress(Exception):
                                task.on_output(task.task_id, line.rstrip())

                    returncode = process.wait()
                finally:
                    watchdog.cancel()

                task.returncode = returncode
                task.ended_at = datetime.now()
                # 已取消的任务保持 CANCELLED，不被覆盖为 COMPLETED/FAILED
                if task.status != TaskStatus.CANCELLED:
                    if timed_out:
                        task.status = TaskStatus.FAILED
                    else:
                        task.status = TaskStatus.COMPLETED if returncode == 0 else TaskStatus.FAILED
                task.process = None

                # 数据版本总线：任务成功完成后通知前端各页面刷新（数据刷新/信号/回测等
                # 会改写 reports/、data/ 下的产出，页面静态数据需要更新）
                if task.status == TaskStatus.COMPLETED:
                    with suppress(Exception):
                        import data_bus

                        data_bus.bump(task.family)

                if task.on_complete:
                    with suppress(Exception):
                        task.on_complete(task.task_id, returncode)

            except Exception as e:
                task.output_lines.append(f"错误: {e}")
                task.returncode = -1
                task.ended_at = datetime.now()
                if task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.FAILED
                if task.on_complete:
                    with suppress(Exception):
                        task.on_complete(task.task_id, -1)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


# 全局任务队列单例
task_queue = TaskQueue()


def _kill_process_tree(process: subprocess.Popen) -> None:
    """杀掉整个进程树：Windows 下 terminate() 可能只杀启动器而遗留子进程。"""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=10,
            )
            # taskkill 失败时可能只返回非零码而不抛异常；等待确认，仍存活就
            # 直接终止当前 Python 进程，避免取消/超时任务永久卡在 RUNNING。
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        else:
            import signal
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        with suppress(Exception):
            process.kill()


def get_task_artifacts(task_id: str, since: datetime | None = None) -> str:
    """获取任务产出文件清单

    Args:
        task_id: 任务类型ID（如 'signal'）
        since: 只列出该时间之后修改的文件（默认用任务开始时间）

    Returns:
        产出文件 Markdown 列表，无产出时返回提示
    """
    tpl = TASKS.get(task_id, {})
    output_patterns = tpl.get("outputs", {})
    result_tab = tpl.get("result_tab", "")
    if not output_patterns:
        return ""

    import glob as _glob

    # 默认：找该任务最近一次实例的开始时间
    if since is None:
        candidates = [t for t in task_queue.tasks.values() if t.task_id.startswith(task_id)]
        started = [t.started_at for t in candidates if t.started_at]
        since = datetime.now() - timedelta(hours=1) if not started else max(started)

    lines = [f"**📦 任务产出**（结果请查看 **{result_tab}** 页签）\n"]
    found_any = False
    for label, pattern in output_patterns.items():
        files = sorted(_glob.glob(pattern), key=os.path.getmtime)
        new_files = [f for f in files
                     if datetime.fromtimestamp(os.path.getmtime(f)) >= since]
        for f in new_files[-3:]:  # 每类最多显示3个最新
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            size_kb = os.path.getsize(f) / 1024
            lines.append(f"- `{label}`: `{f}` ({size_kb:.1f} KB, {mtime.strftime('%H:%M:%S')})")
            found_any = True

    if not found_any:
        lines.append("- *未检测到新产出文件（可能任务无文件输出或输出到其他位置）*")

    return "\n".join(lines)


# 兼容旧接口
def run_task(task_name: str, on_output=None, on_complete=None) -> tuple[bool, str]:
    """提交任务（兼容旧接口）"""
    ok, msg, _ = task_queue.submit(task_name, on_output, on_complete)
    return ok, msg


def get_task_status() -> str:
    """获取任务状态摘要"""
    return task_queue.get_status_summary()
