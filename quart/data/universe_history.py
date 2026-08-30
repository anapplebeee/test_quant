"""成分股历史（Point-in-Time 股票池）。

为什么需要它
------------
`universe.get_constituents()` 只缓存**当日**快照（`{index}_{today}.parquet`）。
用它跑 2020 年的回测，等于用 2026 年的沪深300 成分股——这是标准的前视偏差，
A 股实测量级通常在 3-8pp/yr，**远大于**本项目已花大力气修掉的退市股偏差
（-2.0~-2.6pp/yr）。

本模块提供带生效区间的成分股变更记录，回测按日做 PIT 截面查询。

数据获取
--------
沪深300/中证500 等指数的历史成分股没有免费的逐日 API。可用来源：
  * 中证指数官网每次调样的公告（半年度）
  * akshare `index_stock_cons_weight_csindex` 提供当期权重
  * 商业数据商（Wind/Choice/Tushare）

在拿到权威历史前，本模块支持**由本地数据反推的保守近似**：
一只股票只有在本地仓库有该日行情时才是"可选"的，配合 `require_history_days`
可以要求其在过去 N 日有连续行情。这不能还原真实的调样日期，但能排除
"当时根本没上市/没数据"的股票，是严格的改进。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

#: 变更记录文件名
HISTORY_FILE = "constituents_history.parquet"

#: 变更记录列
HISTORY_COLUMNS = ("symbol", "in_date", "out_date")

#: out_date 为空表示"至今仍在池内"
_OPEN_END = pd.Timestamp("2262-01-01")


def history_path(index_code: str) -> Path:
    return data_root() / "universe" / f"{index_code}_{HISTORY_FILE}"


def save_history(index_code: str, changes: pd.DataFrame) -> Path:
    """保存成分股变更记录。

    Parameters
    ----------
    changes:
        列含 symbol / in_date / out_date。out_date 可为空（表示仍在池内）。
    """
    df = changes.copy()
    missing = set(HISTORY_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"changes 缺少列: {sorted(missing)}")
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["in_date"] = pd.to_datetime(df["in_date"])
    df["out_date"] = pd.to_datetime(df["out_date"]).fillna(_OPEN_END)
    df = df.sort_values(["symbol", "in_date"]).reset_index(drop=True)
    path = history_path(index_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    logger.info("constituents history saved: {} rows -> {}", len(df), path)
    return path


def load_history(index_code: str) -> pd.DataFrame | None:
    """读取成分股变更记录；不存在时返回 None。"""
    path = history_path(index_code)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def constituents_at(index_code: str, date: str | pd.Timestamp) -> list[str] | None:
    """查询某日的成分股（PIT）。无历史记录时返回 None，由调用方决定回退策略。"""
    hist = load_history(index_code)
    if hist is None or hist.empty:
        return None
    d = pd.Timestamp(date)
    mask = (hist["in_date"] <= d) & (hist["out_date"] >= d)
    return sorted(hist.loc[mask, "symbol"].unique().tolist())


def build_history_from_snapshots(index_code: str, snapshots: dict[str, list[str]]) -> pd.DataFrame:
    """由多次采集到的成分股快照反推变更记录。

    Parameters
    ----------
    snapshots:
        {采集日期 YYYYMMDD 或 ISO 日期: 成分股列表}

    算法：把每个快照日视为一次观测，相邻两次观测的差异即在该区间内发生的
    调入/调出。这是**下界近似**——区间内先调出再调入的股票会被漏掉。
    """
    if not snapshots:
        raise ValueError("snapshots 为空")
    obs = sorted((pd.Timestamp(k), set(map(str, v))) for k, v in snapshots.items())
    rows: list[dict] = []
    prev_date: pd.Timestamp | None = None
    prev_set: set[str] = set()

    for date, current in obs:
        if prev_date is not None:
            for sym in sorted(current - prev_set):
                rows.append({"symbol": sym, "in_date": date, "out_date": pd.NaT})
            for sym in sorted(prev_set - current):
                # 回补该股在此前记录中的 out_date
                for r in reversed(rows):
                    if r["symbol"] == sym and pd.isna(r["out_date"]):
                        r["out_date"] = date
                        break
        else:
            for sym in sorted(current):
                rows.append({"symbol": sym, "in_date": date, "out_date": pd.NaT})
        prev_date, prev_set = date, current

    df = pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))
    return df.sort_values(["symbol", "in_date"]).reset_index(drop=True)


def scan_existing_snapshots(index_code: str) -> dict[str, list[str]]:
    """扫描 data/universe/ 下已缓存的 `{index}_{YYYY-MM-DD}.parquet` 快照。

    项目长期运行会累积每日快照，这些是构建 PIT 历史的免费素材。
    """
    udir = data_root() / "universe"
    if not udir.exists():
        return {}
    out: dict[str, list[str]] = {}
    prefix = f"{index_code}_"
    for p in sorted(udir.glob(f"{index_code}_*.parquet")):
        stem = p.stem[len(prefix):]
        try:
            date = dt.date.fromisoformat(stem)
        except ValueError:
            continue  # 非日期后缀（如 constituents_history）
        df = pd.read_parquet(p)
        if "symbol" not in df.columns:
            continue
        out[date.isoformat()] = df["symbol"].astype(str).str.zfill(6).tolist()
    return out


def describe(index_code: str) -> str:
    """人类可读的 PIT 覆盖情况，用于回测报告标注偏差等级。"""
    hist = load_history(index_code)
    if hist is None:
        return f"{index_code}: 无成分股历史，回测使用当前快照（存在前视偏差）"
    open_end = int((hist["out_date"] >= _OPEN_END).sum())
    return (
        f"{index_code}: PIT 历史 {len(hist)} 条记录，"
        f"覆盖 {hist['in_date'].min().date()} ~ {hist['out_date'].max().date()}，"
        f"其中 {open_end} 只仍在池内"
    )


__all__ = [
    "HISTORY_FILE",
    "build_history_from_snapshots",
    "constituents_at",
    "describe",
    "history_path",
    "load_history",
    "save_history",
    "scan_existing_snapshots",
]
