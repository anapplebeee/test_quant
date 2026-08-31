"""全市场换手率/流通市值/估值因子回填（baostock，PIT 不复权口径）。

动机：本地 bars 只有价量字段，缺失 A 股研报中最主流的小市值、流动性（换手率）、
价值（EP/BP）风格因子所需数据。baostock 提供逐日换手率、peTTM、pbMRQ（均为
时点真实值，不受复权基准漂移影响），流通市值由 成交量/换手率×不复权价 推导。

写入约定：
  - 输出 data/factors/fundamental_daily.parquet（长表：date, symbol, turn,
    float_mcap, pe_ttm, pb, is_st），不触碰 data/daily 现有 bars（避免 qfq 漂移）
  - 每 --chunk 只合并落盘一次；中断后重跑自动跳过已覆盖的代码（断点续传）
  - adjustflag=3（不复权）：turn/pe/pb 本身与复权无关，流通市值必须用不复权价

并行：baostock 是单 TCP 连接的阻塞式协议，多线程不能共享一条连接，因此用多进程：
每个 worker 独立 bs.login()、写各自的 part 文件（每 --chunk 落盘一次），主进程
结束时合并进主文件。中断重跑会跳过主文件与各 part 文件中已覆盖的代码。
注意：多会话并发可能触发服务端限流，--workers 建议不超过 8。

用法：.venv/Scripts/python.exe scripts/backfill_factor_data.py [--start 2019-01-01] [--workers 4]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import multiprocessing as mp
import time
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console

from quart.config import data_root, load_config
from quart.data.store import BarStore

console = Console()

FIELDS = "date,close,volume,turn,peTTM,pbMRQ,isST"
OUTPUT_COLUMNS = ["date", "symbol", "turn", "float_mcap", "pe_ttm", "pb", "is_st"]


def baostock_code(symbol: str) -> str | None:
    if symbol.startswith("6"):
        return f"sh.{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz.{symbol}"
    if symbol.startswith(("4", "8", "9")):  # 北交所/B 股 baostock 不覆盖
        return None
    return None


def fetch_symbol(bs, symbol: str, start: str, end: str) -> pd.DataFrame | None:
    code = baostock_code(symbol)
    if code is None:
        return None
    rs = bs.query_history_k_data_plus(
        code, FIELDS, start_date=start, end_date=end, frequency="d", adjustflag="3"
    )
    rows: list[list[str]] = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "close", "volume", "turn", "pe_ttm", "pb", "is_st"])
    df["date"] = pd.to_datetime(df["date"])
    for col in ("close", "volume", "turn", "pe_ttm", "pb"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "turn"])
    df = df[(df["close"] > 0) & (df["turn"] > 0) & (df["volume"] > 0)]
    if df.empty:
        return None
    # 流通股本 = 成交量 / 换手率；流通市值 = 股本 × 不复权收盘价（时点真实值）
    df["float_mcap"] = df["volume"] / (df["turn"] / 100.0) * df["close"]
    df["is_st"] = df["is_st"].astype(str).eq("1")
    df["symbol"] = symbol
    return df[OUTPUT_COLUMNS]


def load_existing_done(output_path: Path) -> set[str]:
    done: set[str] = set()
    paths = [output_path, *sorted(output_path.parent.glob("fundamental_daily.part-*.parquet"))]
    for path in paths:
        if not path.exists():
            continue
        with contextlib.suppress(Exception):
            done |= set(pd.read_parquet(path, columns=["symbol"])["symbol"].unique())
    return done


def run_shard(
    worker_id: int, symbols: list[str], start: str, end: str, part_path: str, chunk: int
) -> tuple[int, int]:
    import socket

    import baostock as bs

    # baostock 阻塞式 recv 无内置超时：挂死会让该进程静默停滞
    socket.setdefaulttimeout(30)
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"worker{worker_id} baostock 登录失败: {lg.error_msg}")

    ok, fail = 0, 0
    consecutive_fail = 0
    pending: list[pd.DataFrame] = []
    start_time = time.time()
    try:
        for i, symbol in enumerate(symbols, 1):
            try:
                df = fetch_symbol(bs, symbol, start, end)
            except Exception as exc:
                logger.warning("worker{} fetch {} failed: {}", worker_id, symbol, exc)
                with contextlib.suppress(Exception):
                    bs.logout()
                time.sleep(2)
                lg = bs.login()
                if lg.error_code != "0":
                    raise RuntimeError(
                        f"worker{worker_id} baostock 重新登录失败: {lg.error_msg}"
                    ) from exc
                df = None
            if df is not None and not df.empty:
                pending.append(df)
                ok += 1
                consecutive_fail = 0
            else:
                fail += 1
                consecutive_fail += 1
                # 服务端限流时连续失败：退避等待配额恢复，避免空转刷错误
                if consecutive_fail % 10 == 0:
                    print(f"[worker{worker_id}] 连续 {consecutive_fail} 次失败，疑似限流，暂停 5 分钟", flush=True)
                    time.sleep(300)
                elif consecutive_fail == 3:
                    print(f"[worker{worker_id}] 连续 3 次失败，疑似限流，暂停 60 秒", flush=True)
                    time.sleep(60)
            if i % chunk == 0 or i == len(symbols):
                if pending:
                    merged = pd.concat(pending, ignore_index=True)
                    pending = []
                    if Path(part_path).exists():
                        merged = pd.concat(
                            [pd.read_parquet(part_path), merged], ignore_index=True
                        )
                    merged = merged.drop_duplicates(subset=["date", "symbol"], keep="last")
                    merged.to_parquet(part_path, index=False)
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0.0
                eta = (len(symbols) - i) / rate if rate > 0 else 0.0
                print(
                    f"[worker{worker_id} {i}/{len(symbols)}] ok={ok} fail={fail} "
                    f"速率 {rate:.2f} 只/秒，剩余约 {eta / 60:.0f} 分钟",
                    flush=True,
                )
    finally:
        with contextlib.suppress(Exception):
            bs.logout()
    return ok, fail


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill turnover/mcap/valuation factor data")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--chunk", type=int, default=250, help="每多少只合并落盘一次")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数（各自独立 baostock 连接）")
    parser.add_argument("--max", type=int, default=None, help="限制回填数量（调试用）")
    args = parser.parse_args()

    end = dt.date.today().isoformat()
    output_dir = data_root() / "factors"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "fundamental_daily.parquet"

    symbols = BarStore().symbols()
    data_cfg = load_config().get("data", {})
    if data_cfg.get("exclude_star", True):
        symbols = [s for s in symbols if not s.startswith(("688", "689"))]
    if data_cfg.get("exclude_chinext", True):
        symbols = [s for s in symbols if not s.startswith(("300", "301"))]
    done = load_existing_done(output_path)
    todo = [s for s in symbols if s not in done]
    if args.max:
        todo = todo[: args.max]
    console.print(f"本地股票 {len(symbols)} 只（按板块过滤后），已覆盖 {len(done)}，待回填 {len(todo)}")
    if not todo:
        return

    workers = max(1, min(args.workers, 8, len(todo)))
    shards = [todo[i::workers] for i in range(workers)]
    part_paths = [output_dir / f"fundamental_daily.part-{w}.parquet" for w in range(workers)]
    console.print(f"{workers} 进程并行回填，每进程约 {len(shards[0])} 只")

    if workers == 1:
        run_shard(0, shards[0], args.start, end, str(part_paths[0]), args.chunk)
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            async_results = [
                pool.apply_async(
                    run_shard, (w, shards[w], args.start, end, str(part_paths[w]), args.chunk)
                )
                for w in range(workers)
            ]
            # worker 异常在此上抛并终止其余进程；part 文件已落盘，重跑自动续传
            for w, result in enumerate(async_results):
                ok, fail = result.get()
                console.print(f"worker{w} 结束: ok={ok} fail={fail}")

    frames = []
    if output_path.exists():
        frames.append(pd.read_parquet(output_path))
    for part in part_paths:
        if part.exists():
            frames.append(pd.read_parquet(part))
            part.unlink()
    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset=["date", "symbol"], keep="last")
        merged.to_parquet(output_path, index=False)

    total = pd.read_parquet(output_path)
    console.print(
        f"[green]完成[/green] 覆盖 {total['symbol'].nunique()} 只代码，"
        f"{len(total)} 行，区间 {total['date'].min().date()} ~ {total['date'].max().date()}"
    )


if __name__ == "__main__":
    main()
