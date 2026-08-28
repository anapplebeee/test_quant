"""退市股历史行情回填（消除幸存者偏差，Phase 1 收官项）。

数据源：
  - 名单：akshare stock_info_sh_delist / stock_info_sz_delist（交易所官方终止上市表）
  - 行情：baostock query_history_k_data_plus（免费、覆盖已退市股票，前复权 adjustflag=2）
    退市股无未来分红/股本变动，其前复权价永久稳定，无 qfq 漂移问题。

写入约定：
  - 与现存储完全一致：data/daily/{symbol}.parquet，BAR_COLUMNS 八列
  - updater 只刷新"当前在市名单"，退市股不在名单内 => 永不被覆盖/删除
  - 从 2018-01-01 起拉取（窗口 2020 前留 2 年），使 min_list_days 次新股过滤自然放行
  - 回填后 force_refresh 重建 list_dates 缓存
  - 幸存者偏差量化见 scripts/measure_survivorship_bias.py

用法：.venv/Scripts/python.exe scripts/backfill_delisted.py [--min-delist-date 2019-01-01]
"""
from __future__ import annotations

import argparse
import datetime as dt

import akshare as ak
import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from quart.data.store import BAR_COLUMNS, BarStore

console = Console()


def fetch_delist_list() -> pd.DataFrame:
    frames = []
    sh = ak.stock_info_sh_delist()
    frames.append(
        pd.DataFrame(
            {
                "symbol": sh["公司代码"].astype(str).str.zfill(6),
                "name": sh["公司简称"],
                "delist_date": pd.to_datetime(sh["暂停上市日期"]),
            }
        )
    )
    sz = ak.stock_info_sz_delist()
    frames.append(
        pd.DataFrame(
            {
                "symbol": sz["证券代码"].astype(str).str.zfill(6),
                "name": sz["证券简称"],
                "delist_date": pd.to_datetime(sz["终止上市日期"]),
            }
        )
    )
    out = pd.concat(frames, ignore_index=True).drop_duplicates("symbol")
    return out.sort_values("delist_date").reset_index(drop=True)


def baostock_code(symbol: str) -> str:
    return ("sh." if symbol.startswith("6") else "sz.") + symbol


def fetch_bars(bs, symbol: str, start: str, end: str) -> pd.DataFrame | None:
    rs = bs.query_history_k_data_plus(
        baostock_code(symbol),
        "date,open,high,low,close,volume,amount",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="2",  # 前复权
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close", "open"])
    df = df[df["close"] > 0]
    return df[BAR_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill delisted stocks daily bars")
    parser.add_argument("--min-delist-date", default="2019-01-01")
    parser.add_argument("--start", default="2018-01-01", help="行情起始（早于回测窗口以放行次新股过滤）")
    args = parser.parse_args()

    delisted = fetch_delist_list()
    console.print(f"交易所退市名单合计: {len(delisted)} 只")
    todo = delisted[delisted["delist_date"] >= args.min_delist_date].copy()
    # 仅回填沪深 A 股主板（6/0 开头）；B 股(200/900)、三板(4/8)、创业板科创板(300/301/688)排除：
    # B股/三板 baostock 不覆盖，创/科板本就被 filter_for_simulation 排除，回填无意义
    todo = todo[todo["symbol"].str.startswith(("6", "0"))].copy()
    console.print(f"退市日晚于 {args.min_delist_date} 且属主板 A 股: {len(todo)} 只（回填范围）")

    store = BarStore()
    delisted.to_csv(store.universe_dir / "delisted.csv", index=False, encoding="utf-8-sig")
    console.print(f"[green]名单已存: {store.universe_dir / 'delisted.csv'}[/green]")

    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock 登录失败: {lg.error_msg}")

    ok, skip, fail = 0, 0, []
    stats = []
    try:
        for _, row in todo.iterrows():
            sym = row["symbol"]
            path = store.daily_dir / f"{sym}.parquet"
            if path.exists():
                existing = pd.read_parquet(path)
                if len(existing) >= 50:
                    skip += 1
                    continue
            try:
                df = fetch_bars(bs, sym, args.start, dt.date.today().isoformat())
                if df is None or df.empty:
                    fail.append((sym, "empty"))
                    continue
                store.save(df, replace=True)
                ok += 1
                stats.append((sym, str(row["name"]), len(df), str(df["date"].iloc[-1].date()), float(df["close"].iloc[-1])))
            except Exception as exc:  # noqa: BLE001
                fail.append((sym, str(exc)))
    finally:
        bs.logout()

    # 重建 list_dates 缓存（把退市股的真实历史首日纳入次新股过滤）
    from quart.data.universe import get_list_dates

    get_list_dates(force_refresh=True)

    t = Table(title=f"回填结果: 成功 {ok} | 跳过(已存在) {skip} | 失败 {len(fail)}")
    for c in ["代码", "名称", "行数", "最后交易日", "最后收盘"]:
        t.add_column(c, justify="right")
    for sym, name, n, last_dt, last_px in stats[:20]:
        t.add_row(sym, name, str(n), last_dt, f"{last_px:.2f}")
    if len(stats) > 20:
        t.add_row("...", f"等 {len(stats)} 只", "", "", "")
    console.print(t)
    if fail:
        console.print(f"[yellow]失败明细: {fail[:10]}[/yellow]")
    logger.info("delisted backfill done: ok={} skip={} fail={}", ok, skip, len(fail))


if __name__ == "__main__":
    main()
