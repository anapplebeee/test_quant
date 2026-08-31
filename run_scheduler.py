from __future__ import annotations

import datetime as dt

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from loguru import logger

from quart.config import PROJECT_ROOT, load_config
from quart.data.universe import filter_st, get_constituents
from quart.data.updater import update_universe_data
from quart.pipeline import run_daily


def is_trading_day(today: dt.date | None = None) -> bool:
    """交易日判断：优先 akshare 交易日历（本地缓存），失败回退周一~周五。

    避免节假日照常生成信号：节前旧收盘价输出的交易计划对使用者有误导。
    """
    d = today or dt.date.today()
    cache = PROJECT_ROOT / "data" / "trade_dates.parquet"
    try:
        if cache.exists():
            dates = set(pd.to_datetime(pd.read_parquet(cache)["trade_date"]).dt.date)
        else:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            cache.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache, index=False)
            dates = set(df["trade_date"])
        return d in dates
    except Exception as exc:
        logger.warning("trade calendar unavailable, fallback to weekday check: {}", exc)
        return d.weekday() < 5


def job_update() -> None:
    if not is_trading_day():
        logger.info("non-trading day, skip data update")
        return
    logger.info("=== data update start ===")
    try:
        cfg = load_config()
        universe_mode = cfg.get("universe", {}).get("mode", "index")
        index_code = cfg["universe"]["default_index"]
        workers = int(cfg.get("universe", {}).get("workers", 1))
        if universe_mode == "all":
            from quart.data.source_akshare import fetch_stock_list
            codes = fetch_stock_list()["symbol"].tolist()
        elif universe_mode == "mainboard":
            from quart.data.source_akshare import fetch_stock_list
            from quart.data.universe import filter_mainboard
            codes = filter_mainboard(fetch_stock_list()["symbol"].tolist())
        else:
            codes = get_constituents(index_code)
        try:
            codes = filter_st(codes)
        except Exception as exc:
            logger.warning("ST filter skipped: {}", exc)
        stats = update_universe_data(index_code, codes, start="20190101", workers=workers)
        logger.info("update done: {}", stats)
    except Exception:
        logger.exception("update failed")


def job_signal() -> None:
    if not is_trading_day():
        logger.info("non-trading day, skip signal pipeline")
        return
    logger.info("=== daily pipeline start ===")
    try:
        run_daily()
    except Exception:
        logger.exception("pipeline failed")
        # 失败也要告警：否则信号没发出去无人知晓
        try:
            from quart.notify.dingtalk import send_markdown

            send_markdown("Quart 信号失败告警", "⚠️ 每日信号流水线执行失败，请检查 logs/quart.log")
        except Exception:
            pass


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
