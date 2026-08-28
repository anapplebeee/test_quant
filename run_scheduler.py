from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from quart.config import PROJECT_ROOT, load_config
from quart.data.universe import filter_st, get_constituents
from quart.data.updater import update_universe_data
from quart.pipeline import run_daily


def job_update() -> None:
    logger.info("=== data update start ===")
    try:
        cfg = load_config()
        universe_mode = cfg.get("universe", {}).get("mode", "index")
        index_code = cfg["universe"]["default_index"]
        if universe_mode == "all":
            from quart.data.source_akshare import fetch_stock_list
            codes = fetch_stock_list()["symbol"].tolist()
        else:
            codes = get_constituents(index_code)
        try:
            codes = filter_st(codes)
        except Exception as exc:
            logger.warning("ST filter skipped: {}", exc)
        stats = update_universe_data(index_code, codes, start="20190101")
        logger.info("update done: {}", stats)
    except Exception:
        logger.exception("update failed")


def job_signal() -> None:
    logger.info("=== daily pipeline start ===")
    try:
        run_daily()
    except Exception:
        logger.exception("pipeline failed")


def main() -> None:
    logger.remove()
    logger.add(PROJECT_ROOT / "logs" / "quart.log", rotation="10 MB", retention="30 days", encoding="utf-8")
    logger.add(lambda msg: print(msg, end=""))
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    sched.add_job(job_update, "cron", day_of_week="mon-fri", hour=15, minute=40, id="daily_update")
    sched.add_job(job_signal, "cron", day_of_week="mon-fri", hour=18, minute=30, id="daily_signal")
    jobs = sched.get_jobs()
    logger.info("scheduler started: {}", [f"{j.id}@{j.trigger}" for j in jobs])
    sched.start()


if __name__ == "__main__":
    main()
