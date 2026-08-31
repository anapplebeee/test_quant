"""Job 崩溃恢复演示（协调文档 10.2：Job/Worker 重启恢复测试演练）。

演示：Worker 认领任务后"崩溃"（不续约），新进程启动后 recover() 回收过期任务，
重新 claim 并成功执行。验证持久化 Job 的恢复能力。

用法：
    uv run python scripts/job_recovery_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel

from quart.infrastructure.db import Database
from quart.infrastructure.job import JobRepository

console = Console()


def main() -> None:
    # 用独立临时库演示（避免污染 state/quart.db）
    db = Database(Path("state") / "job_recovery_demo.db")
    repo = JobRepository(db, lease_seconds=5)
    repo.migrate()

    # 1. 创建任务并入队
    job = repo.create("backtest", {"strategy": "lowvol_indz"}, idempotency_key="demo-1")
    console.print(Panel(f"[green]创建 job[/green] {job.job_id} type={job.job_type} status={job.status}"))

    # 2. Worker-1 认领并标记 RUNNING
    claimed = repo.claim("worker-1")
    repo.mark_running(claimed.job_id, "worker-1")
    console.print(f"[yellow]Worker-1 认领并运行[/yellow] status={repo.get(claimed.job_id).status}")

    # 3. 模拟 Worker-1 崩溃：不续约，租约过期
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, claimed.job_id))
    console.print("[red]Worker-1 崩溃[/red]（租约过期，不续约）")

    # 4. 新进程 Worker-2 启动，recover() 回收
    repo2 = JobRepository(db, lease_seconds=5)
    stats = repo2.recover()
    console.print(f"[blue]Worker-2 启动 recover()[/blue] requeued={stats['requeued']} failed={stats['failed']}")

    # 5. Worker-2 重新认领并成功执行
    reclaimed = repo2.claim("worker-2")
    if reclaimed is None:
        console.print("[red]恢复失败：无法重新认领[/red]")
        sys.exit(1)
    repo2.succeed(reclaimed.job_id, "worker-2", {"recovered": True, "attempts": reclaimed.attempts})
    final = repo2.get(claimed.job_id)
    console.print(
        Panel(
            f"[green]恢复并执行成功[/green]\n"
            f"  status={final.status}\n"
            f"  result={final.result}\n"
            f"  总尝试次数={final.attempts}（含崩溃前 1 次）",
            title="JOB-001 恢复演练",
        )
    )
    console.print("\n演示完成：持久化 Job 崩溃后可由新进程恢复重跑。")

    # 清理演示库（可选）
    # db.path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
