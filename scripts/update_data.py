from __future__ import annotations

import argparse
import datetime as dt
import json

from loguru import logger
from rich.console import Console

from quart.config import data_root, load_config
from quart.data.universe import filter_mainboard, filter_st, get_constituents
from quart.data.updater import update_universe_data

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Update local A-share daily bar store")
    parser.add_argument(
        "--universe",
        default="index",
        choices=["index", "all", "mainboard"],
        help="stock pool: index constituents / all A-shares / only mainboard (沪深主板)",
    )
    parser.add_argument("--index", default=load_config()["universe"]["default_index"], help="index code for benchmark, e.g. 000300")
    parser.add_argument("--start", default="20190101", help="history start YYYYMMDD")
    parser.add_argument("--max", type=int, default=None, help="limit number of symbols (debug)")
    parser.add_argument("--workers", type=int, default=8, help="parallel symbol workers (1-32)")
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="re-fetch every selected symbol from --start and replace returned history",
    )
    # 兼容短别名 --full（等价于 --full-refresh）
    parser.add_argument("--full", dest="full_refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--keep-st", action="store_true", help="do not exclude ST names")
    args = parser.parse_args()

    if args.universe == "all":
        from quart.data.source_akshare import fetch_stock_list

        logger.info("fetching full A-share stock list...")
        df = fetch_stock_list()
        codes = df["symbol"].tolist()
        logger.info("full market: {} codes loaded", len(codes))
    elif args.universe == "mainboard":
        from quart.data.source_akshare import fetch_stock_list

        logger.info("fetching full A-share stock list, then filtering to mainboard...")
        df = fetch_stock_list()
        codes = filter_mainboard(df["symbol"].tolist())
        logger.info("mainboard: {} codes (from full market {})", len(codes), len(df))
    else:
        codes = get_constituents(args.index)

    if not args.keep_st:
        try:
            codes = filter_st(codes)
        except Exception as exc:
            logger.warning("ST filter skipped: {}", exc)

    # 并发刷新默认 8 workers：A 股主板 ~3000+ 只，串行按每只 ~0.35s 限速
    # 需要 ~20 分钟；并发后大幅压缩。akshare 的东财/腾讯接口对并发不敏感，
    # 但为避免触发反爬，默认 8（可用 --workers 覆盖，上限 32）。
    workers = max(1, min(int(args.workers), 32))
    logger.info(
        "updating {} symbols (universe={}, benchmark={}, workers={}, full_refresh={})",
        len(codes), args.universe, args.index, workers, args.full_refresh,
    )
    stats = update_universe_data(
        args.index,
        codes,
        start=args.start,
        max_names=args.max,
        workers=workers,
        full_refresh=args.full_refresh,
    )
    meta_dir = data_root() / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        "universe": args.universe,
        "index": args.index,
        "start": args.start,
        "workers": workers,
        "full_refresh": args.full_refresh,
        **stats,
    }
    status_path = meta_dir / "last_data_update.json"
    temp_path = status_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(status_path)
    console.print(
        f"[green]done[/green] total={stats['total']} ok={stats['ok']} "
        f"empty={stats['empty']} failed={stats['failed']} "
        f"refreshed={stats.get('refreshed', 0)}"
    )
    if stats["failed_symbols"]:
        console.print(f"[yellow]failed: {stats['failed_symbols'][:20]}{'...' if len(stats['failed_symbols'])>20 else ''}[/yellow]")


if __name__ == "__main__":
    main()
