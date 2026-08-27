from __future__ import annotations

import argparse

from loguru import logger
from rich.console import Console

from quart.config import load_config
from quart.data.universe import filter_st, get_constituents
from quart.data.updater import update_universe_data

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Update local A-share daily bar store")
    parser.add_argument("--universe", default="index", choices=["index", "all"], help="stock pool: index constituents or all A-shares")
    parser.add_argument("--index", default=load_config()["universe"]["default_index"], help="index code for benchmark, e.g. 000300")
    parser.add_argument("--start", default="20190101", help="history start YYYYMMDD")
    parser.add_argument("--max", type=int, default=None, help="limit number of symbols (debug)")
    parser.add_argument("--keep-st", action="store_true", help="do not exclude ST names")
    args = parser.parse_args()

    if args.universe == "all":
        from quart.data.source_akshare import fetch_stock_list
        logger.info("fetching full A-share stock list...")
        df = fetch_stock_list()
        codes = df["symbol"].tolist()
        logger.info("full market: {} codes loaded", len(codes))
    else:
        codes = get_constituents(args.index)

    if not args.keep_st:
        try:
            codes = filter_st(codes)
        except Exception as exc:
            logger.warning("ST filter skipped: {}", exc)

    logger.info("updating {} symbols (universe={}, benchmark={})", len(codes), args.universe, args.index)
    stats = update_universe_data(args.index, codes, start=args.start, max_names=args.max)
    console.print(f"[green]done[/green] total={stats['total']} ok={stats['ok']} empty={stats['empty']} failed={stats['failed']} refreshed={stats.get('refreshed',0)}")


if __name__ == "__main__":
    main()
