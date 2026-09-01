"""证券状态 PIT 构建器（RESEARCH-002 §8-2，P0）。

把 security_master.SOURCE_MAPPING 中 pending 的字段逐项填实：

- 真实上市日龄：交易所官方列表（沪/深/北 ``*_name_code``，含上市日期）；
- 退市样本：沪深退市证券列表（``stock_info_{sh,sz}_delist``，含摘牌日期）；
- ST/风险警示：当前 ST 快照 + （可选）名称变更历史重建 ST 区间；
- 停复牌：东财停复牌逐日接口（``stock_tfp_em``），按日增量积累区间；
- 逐日指数成分：中证指数官方成分快照落入 universe_history（SCD2 区间表，
  首个快照即生效起点，此后每日重跑自动累积变更）。

产出：
- data/meta/security_master.parquet（状态区间行，PIT 可查询）
- data/meta/security_master_version 自动进入快照 PIT 元数据
- data/universe/constituents_history/<index>.parquet
- data/meta/suspensions.parquet / st_history.parquet

用法：
    uv run python scripts/build_security_master.py                 # 全量构建
    uv run python scripts/build_security_master.py --suspend-since 2024-01-01
    uv run python scripts/build_security_master.py --st-name-history --st-limit 200
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quart.config import data_root
from quart.data.security_master import SecurityMaster
from quart.data.universe_history import load_history, merge_history_snapshot, save_history

console = Console()

META_DIR = Path(data_root()) / "meta"
ST_HISTORY_PATH = META_DIR / "st_history.parquet"
SUSPEND_PATH = META_DIR / "suspensions.parquet"
OBSERVED_AT = pd.Timestamp.now().normalize()


def _retry(fn, retries: int = 3, wait: float = 3.0):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:
            logger.warning("fetch attempt {}/{} failed: {}", attempt, retries, exc)
            if attempt < retries:
                time.sleep(wait * attempt)
    return None


# ---------------- 上市/退市 ----------------


def fetch_listings() -> pd.DataFrame:
    """当前上市证券（真实上市日期），返回 symbol, listed_at, name。"""
    import akshare as ak

    frames = []
    sh = _retry(lambda: ak.stock_info_sh_name_code(symbol="主板A股"))
    star = _retry(lambda: ak.stock_info_sh_name_code(symbol="科创板"))
    if sh is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sh["证券代码"].astype(str).str.zfill(6),
                    "name": sh["证券简称"],
                    "listed_at": pd.to_datetime(sh["上市日期"], errors="coerce"),
                }
            )
        )
    if star is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": star["证券代码"].astype(str).str.zfill(6),
                    "name": star["证券简称"],
                    "listed_at": pd.to_datetime(star["上市日期"], errors="coerce"),
                }
            )
        )
    sz = _retry(lambda: ak.stock_info_sz_name_code(symbol="A股列表"))
    if sz is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sz["A股代码"].astype(str).str.zfill(6),
                    "name": sz["A股简称"],
                    "listed_at": pd.to_datetime(sz["A股上市日期"], errors="coerce"),
                }
            )
        )
    bj = _retry(lambda: ak.stock_info_bj_name_code())
    if bj is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": bj["证券代码"].astype(str).str.zfill(6),
                    "name": bj["证券简称"],
                    "listed_at": pd.to_datetime(bj["上市日期"], errors="coerce"),
                }
            )
        )
    out = pd.concat(frames, ignore_index=True).dropna(subset=["listed_at"])
    out = out.drop_duplicates("symbol", keep="first")
    logger.info("listings fetched: {}", len(out))
    return out


def fetch_delistings() -> pd.DataFrame:
    """退市证券（上市日 + 摘牌日），返回 symbol, listed_at, delisted_at, name。"""
    import akshare as ak

    frames = []
    sh = _retry(lambda: ak.stock_info_sh_delist())
    if sh is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sh["公司代码"].astype(str).str.zfill(6),
                    "name": sh["公司简称"],
                    "listed_at": pd.to_datetime(sh["上市日期"], errors="coerce"),
                    "delisted_at": pd.to_datetime(sh["暂停上市日期"], errors="coerce"),
                }
            )
        )
    sz = _retry(lambda: ak.stock_info_sz_delist())
    if sz is not None:
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sz["证券代码"].astype(str).str.zfill(6),
                    "name": sz["证券简称"],
                    "listed_at": pd.to_datetime(sz["上市日期"], errors="coerce"),
                    "delisted_at": pd.to_datetime(sz["终止上市日期"], errors="coerce"),
                }
            )
        )
    out = pd.concat(frames, ignore_index=True).dropna(subset=["delisted_at"])
    out = out.drop_duplicates("symbol", keep="first")
    logger.info("delistings fetched: {}", len(out))
    return out


# ---------------- ST ----------------


def fetch_st_snapshot() -> pd.DataFrame:
    """当前 ST/风险警示名单（符号级）。"""
    import akshare as ak

    df = _retry(lambda: ak.stock_zh_a_st_em(), retries=4)
    if df is None or df.empty:
        logger.warning("ST snapshot unavailable")
        return pd.DataFrame(columns=["symbol", "name"])
    return pd.DataFrame(
        {
            "symbol": df["代码"].astype(str).str.zfill(6),
            "name": df["名称"],
        }
    ).drop_duplicates("symbol")


def fetch_st_history(limit: int | None) -> pd.DataFrame:
    """经名称变更历史重建 ST 区间（可选，逐股抓取、断点续传）。

    名称含 ``ST``/``*ST``/``退`` 视为风险警示状态生效；恢复日 = 下一次
    变更去掉 ST 的日期。仅覆盖抓取过的符号，其余回退到当前快照。
    """
    import akshare as ak

    cache = ST_HISTORY_PATH
    done: dict[str, list[dict]] = {}
    if cache.exists():
        old = pd.read_parquet(cache)
        for sym, g in old.groupby("symbol"):
            done[sym] = g.to_dict("records")
    st_now = fetch_st_snapshot()
    targets = st_now["symbol"].tolist()
    todo = [s for s in targets if s not in done]
    if limit:
        todo = todo[:limit]
    for n, sym in enumerate(todo, 1):
        rows = []
        try:
            hist = ak.stock_info_change_name(symbol=sym)
            if hist is not None and not hist.empty:
                rows = _name_history_to_st_rows(sym, hist)
        except Exception as exc:
            logger.warning("name history {} failed: {}", sym, exc)
        done[sym] = rows or [
            {
                "symbol": sym,
                "status": "st",
                "status_effective_from": pd.NaT,
                "status_effective_to": pd.NaT,
                "observed_at": OBSERVED_AT,
                "source": "snapshot_fallback",
            }
        ]
        if n % 20 == 0:
            _save_st_cache(done)
            logger.info("st history progress {}/{}", n, len(todo))
        time.sleep(0.5)
    _save_st_cache(done)
    return pd.DataFrame([r for rows in done.values() for r in rows])


def _name_history_to_st_rows(symbol: str, hist: pd.DataFrame) -> list[dict]:
    """把名称变更历史转成 ST 状态区间（含头不含尾）。"""
    date_col = next((c for c in ("变更日期", "日期", "date") if c in hist.columns), None)
    name_col = next((c for c in ("变更后名称", "新名称", "证券简称", "name") if c in hist.columns), None)
    if date_col is None or name_col is None:
        return []
    events = []
    for _, r in hist.iterrows():
        d = pd.to_datetime(r[date_col], errors="coerce")
        if pd.isna(d):
            continue
        name = str(r[name_col])
        is_st = ("ST" in name.upper()) or ("退" in name)
        events.append((d, is_st))
    events.sort()
    rows = []
    for i, (d, is_st) in enumerate(events):
        nxt = events[i + 1][0] if i + 1 < len(events) else pd.NaT
        rows.append(
            {
                "symbol": symbol,
                "status": "st" if is_st else "normal",
                "status_effective_from": d,
                "status_effective_to": nxt,
                "observed_at": OBSERVED_AT,
                "source": "name_history",
            }
        )
    return rows


def _save_st_cache(done: dict[str, list[dict]]) -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([r for rows in done.values() for r in rows]).to_parquet(ST_HISTORY_PATH, index=False)


# ---------------- 停复牌 ----------------


def fetch_suspensions(since: str) -> pd.DataFrame:
    """东财停复牌逐日快照 → 停牌区间表（断点续传）。"""
    import akshare as ak

    cache = SUSPEND_PATH
    have: set[str] = set()
    if cache.exists():
        have = set(pd.read_parquet(cache)["snap_date"].astype(str))
    dates = pd.bdate_range(since, pd.Timestamp.now().date()).strftime("%Y%m%d").tolist()
    todo = [d for d in dates if d not in have]
    frames = []
    for n, d in enumerate(todo, 1):
        try:
            df = ak.stock_tfp_em(date=d)
            if df is not None and not df.empty:
                code_col = next((c for c in ("代码", "股票代码", "证券代码") if c in df.columns), None)
                in_col = next((c for c in ("停牌时间", "停牌起始日", "suspen_starttime") if c in df.columns), None)
                out_col = next((c for c in ("复牌时间", "复牌日期", "resumptiontime") if c in df.columns), None)
                reason_col = next((c for c in ("停牌原因", "suspen_reason") if c in df.columns), None)
                frames.append(
                    pd.DataFrame(
                        {
                            "snap_date": d,
                            "symbol": df[code_col].astype(str).str.zfill(6),
                            "suspend_from": pd.to_datetime(df[in_col], errors="coerce") if in_col else pd.NaT,
                            "resume_at": pd.to_datetime(df[out_col], errors="coerce") if out_col else pd.NaT,
                            "reason": df[reason_col] if reason_col else "",
                        }
                    )
                )
        except Exception as exc:
            if n % 50 == 0:
                logger.warning("tfp {} failed: {}", d, exc)
        if n % 30 == 0:
            _append_suspensions(cache, frames)
            frames = []
            logger.info("suspension progress {}/{}", n, len(todo))
        time.sleep(0.4)
    _append_suspensions(cache, frames)
    return pd.read_parquet(cache) if cache.exists() else pd.DataFrame()


def _append_suspensions(cache: Path, frames: list[pd.DataFrame]) -> None:
    if not frames:
        return
    META_DIR.mkdir(parents=True, exist_ok=True)
    new = pd.concat(frames, ignore_index=True)
    old = pd.read_parquet(cache) if cache.exists() else pd.DataFrame()
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["snap_date", "symbol"], keep="last")
    merged.to_parquet(cache, index=False)


# ---------------- 指数成分 ----------------


def fetch_constituents(index_code: str = "000300") -> pd.DataFrame:
    """中证指数官方成分快照 → universe_history SCD2（每日重跑自动累积变更）。"""
    import akshare as ak

    df = _retry(lambda: ak.index_stock_cons_csindex(symbol=index_code), retries=3)
    if df is None or df.empty:
        logger.warning("constituents {} unavailable", index_code)
        return pd.DataFrame()
    sym_col = next((c for c in ("成分券代码", "证券代码", "成分股代码") if c in df.columns), "成分券代码")
    snap = df[sym_col].astype(str).str.zfill(6).tolist()
    observed_at = pd.Timestamp.now().normalize()
    changes = merge_history_snapshot(load_history(index_code), observed_at, snap)
    path = save_history(index_code, changes)
    logger.info("constituents {} saved: {} symbols -> {}", index_code, len(snap), path)
    return changes


# ---------------- 主表装配 ----------------


def build_master(
    listings: pd.DataFrame, delistings: pd.DataFrame, st_rows: pd.DataFrame, suspensions: pd.DataFrame
) -> SecurityMaster:
    """装配状态区间行主数据：每符号至少一行 listed；ST/退市为附加状态行。"""
    st_by_sym = {sym: g for sym, g in (st_rows.groupby("symbol") if not st_rows.empty else [])}
    rows = []

    def add_status(symbol, status, frm, to, rule):
        rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "board": board,
                "security_type": "stock",
                "listed_at": listed,
                "delisted_at": delisted,
                "status": status,
                "status_effective_from": frm,
                "status_effective_to": to,
                "lot_size": 100,
                "tick_size": 0.01,
                "price_limit_rule": rule,
                "settlement_rule": "T+1",
            }
        )

    from quart.data.security_master import _board_of

    def base(symbol, listed, delisted):
        _, _, _, rule = _board_of(symbol)
        return rule, listed, delisted

    for _, r in listings.iterrows():
        symbol = r["symbol"]
        exchange, board, _, rule = _board_of(symbol)
        listed, delisted = r["listed_at"], pd.NaT
        add_status(symbol, "listed", listed, pd.NaT, rule)
        st_g = st_by_sym.get(symbol)
        if st_g is not None and len(st_g):
            for _, s in st_g.iterrows():
                add_status(symbol, s["status"], s["status_effective_from"], s["status_effective_to"], rule)
    for _, r in delistings.iterrows():
        symbol = r["symbol"]
        exchange, board, _, rule = _board_of(symbol)
        listed, delisted = r["listed_at"], r["delisted_at"]
        add_status(symbol, "listed", listed, pd.NaT, rule)
        add_status(symbol, "delisted", delisted, pd.NaT, rule)
        if "ST" in str(r.get("name", "")).upper():
            add_status(symbol, "st", pd.NaT, delisted, rule)

    master = SecurityMaster(pd.DataFrame(rows))
    # 停牌区间单独落盘（主表只存状态，停牌是日历属性）
    if not suspensions.empty:
        suspensions.to_parquet(META_DIR / "suspensions.parquet", index=False)
    return master


def main() -> None:
    parser = argparse.ArgumentParser(description="证券状态 PIT 构建")
    parser.add_argument("--suspend-since", default="2024-01-01", help="停复牌回补起点")
    parser.add_argument("--st-name-history", action="store_true", help="经名称历史重建 ST 区间（逐股）")
    parser.add_argument("--st-limit", type=int, default=None)
    parser.add_argument("--skip-suspend", action="store_true")
    args = parser.parse_args()

    listings = fetch_listings()
    delistings = fetch_delistings()
    if args.st_name_history:
        st_rows = fetch_st_history(args.st_limit)
    else:
        st_now = fetch_st_snapshot()
        # 无名称历史时：当前 ST 名单以快照日为生效起点（保守，宁晚勿早）
        st_rows = pd.DataFrame(
            [
                {
                    "symbol": s,
                    "status": "st",
                    "status_effective_from": OBSERVED_AT,
                    "status_effective_to": pd.NaT,
                    "observed_at": OBSERVED_AT,
                    "source": "snapshot",
                }
                for s in st_now["symbol"]
            ]
        )
    suspensions = pd.DataFrame() if args.skip_suspend else fetch_suspensions(args.suspend_since)
    fetch_constituents("000300")

    master = build_master(listings, delistings, st_rows, suspensions)
    problems = master.validate()
    if problems:
        console.print(f"[yellow]validation: {len(problems)} issues[/yellow]")
        for p in problems[:5]:
            console.print(" ", p)
    path = master.save()
    ver = master.version()
    console.print(f"[green]security_master saved: {len(master.table)} rows -> {path}[/green]")
    console.print(f"security_master_version = {ver}")
    console.print(
        f"listed={int((master.table['status'] == 'listed').sum())} "
        f"st={int((master.table['status'] == 'st').sum())} "
        f"delisted={int((master.table['status'] == 'delisted').sum())} "
        f"suspensions={len(suspensions)}"
    )


if __name__ == "__main__":
    main()
