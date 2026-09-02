"""生成退市清单 ``data/meta/delisted.parquet``，修复退市过滤失效断裂。

背景（数据清洗 2026-09-02）
---------------------------
``quart/data/delisted.py`` 依赖 ``data/meta/delisted.parquet``（列
code/name/delisted_at）做退市日裁剪，但该文件缺失、且此前没有任何脚本产出它
（delisted.py 自身 docstring 记为“真实缺口”）。缺失时 ``load_delisted`` 返回空
清单 → ``filter_delisted_bars`` 不触发 → 数据源把已退市代码的“幽灵行情”写回
``daily`` 后，回测会把退市日后行情当真实可交易标的，产生幸存者偏差 +
不可成交持仓。实测 361 只退市股中 **195 只仍残留在 daily/**。

数据源
------
本地权威清单 ``data/universe/delisted.csv``（symbol/name/delist_date，由
``scripts/backfill_delisted.py`` 从交易所官方终止上市表写入，361 只，最新至
2026-07-17）。本脚本只做 **schema 对齐 + 转存**，不重复抓官方源，避免依赖
akshare 不稳定接口；加 ``--refresh`` 才调用 akshare 官方退市表刷新名单。

用法
----
    .venv/Scripts/python.exe scripts/build_delisted_list.py [--refresh]
"""
from __future__ import annotations

import argparse

import pandas as pd
from loguru import logger

from quart.data.delisted import DELISTED_COLUMNS, DELISTED_PATH
from quart.data.store import BarStore

_SOURCE = "delisted.csv"
_SOURCE_COLS = {"symbol": "code", "name": "name", "delist_date": "delisted_at"}


def _local_frame(store: BarStore) -> pd.DataFrame | None:
    src = store.universe_dir / _SOURCE
    if not src.exists():
        logger.warning("delisted source missing: {}（先运行 scripts/backfill_delisted.py）", src)
        return None
    df = pd.read_csv(src)
    missing = [c for c in _SOURCE_COLS if c not in df.columns]
    if missing:
        logger.warning("delisted source {} missing columns {}", src, missing)
        return None
    out = df[_SOURCE_COLS.keys()].rename(columns=_SOURCE_COLS).copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    out["delisted_at"] = pd.to_datetime(out["delisted_at"], errors="coerce")
    out["name"] = out["name"].astype(str)
    out = out.dropna(subset=["code", "delisted_at"]).drop_duplicates("code")
    return out.sort_values("delisted_at").reset_index(drop=True)


def refresh_from_akshare(store: BarStore) -> pd.DataFrame:
    """（可选）调用交易所官方退市表刷新名单，覆写 delisted.csv 后返回。"""
    import akshare as ak

    frames = []
    sh = ak.stock_info_sh_delist()
    frames.append(
        pd.DataFrame(
            {
                "symbol": sh["公司代码"].astype(str).str.zfill(6),
                "name": sh["公司简称"].astype(str),
                "delist_date": pd.to_datetime(sh["暂停上市日期"], errors="coerce"),
            }
        )
    )
    sz = ak.stock_info_sz_delist()
    frames.append(
        pd.DataFrame(
            {
                "symbol": sz["证券代码"].astype(str).str.zfill(6),
                "name": sz["证券简称"].astype(str),
                "delist_date": pd.to_datetime(sz["终止上市日期"], errors="coerce"),
            }
        )
    )
    out = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("symbol")
        .sort_values("delist_date")
        .reset_index(drop=True)
    )
    out.to_csv(store.universe_dir / _SOURCE, index=False, encoding="utf-8-sig")
    logger.info("refreshed {} delisted symbols -> {}", len(out), store.universe_dir / _SOURCE)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build data/meta/delisted.parquet")
    parser.add_argument("--refresh", action="store_true", help="先调 akshare 官方退市表刷新名单")
    args = parser.parse_args()

    store = BarStore()
    if args.refresh:
        refresh_from_akshare(store)
    frame = _local_frame(store)
    if frame is None:
        raise SystemExit("无法生成 delisted.parquet：本地退市清单缺失或损坏（fail-closed，不产出空清单）")

    DELISTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    frame[DELISTED_COLUMNS].to_parquet(DELISTED_PATH, index=False)
    logger.info("delisted.parquet written: {} symbols -> {}", len(frame), DELISTED_PATH)


if __name__ == "__main__":
    main()
