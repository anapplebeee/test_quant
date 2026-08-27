from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from quart.config import PROJECT_ROOT
from quart.pipeline import run_daily


def job() -> None:
    logger.info("=== daily pipeline start ===")
    try:
        run_daily()
    except Exception as exc:
        logger.exception("pipeline failed: {}", exc)


def main() -> None:
    logger.remove()
    logger.add(PROJECT_ROOT / "logs" / "quart.log", rotation="10 MB", retention="30 days", encoding="utf-8")
    logger.add(lambda msg: print(msg, end=""))
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    sched.add_job(job, "cron", day_of_week="mon-fri", hour=17, minute=30, id="daily_signal")
    logger.info("scheduler started, next run: {}", sched.get_jobs()[0].next_run_time)
    sched.start()


if __name__ == "__main__":
    main()
