"""退市清单与退市日裁剪（轻量防线，不依赖 security_master）。

真实缺口（RESEARCH 2026-09 核查）：
    ``data/meta/security_master.parquet`` 不存在 → ``rule_resolver`` 的退市
    检查永远不触发；一旦数据源把已退市代码的行情写回 ``daily`` 分区（部分
    数据源对退市代码返回过期/错位数据），回测会把"幽灵行情"当作真实可交易
    标的，产生幸存者偏差 + 不可成交持仓。本模块提供统一的退市清单加载与
    按退市日裁剪，作为数据层的独立防线。

清单来源：
    ``data/meta/delisted.parquet``（列: code, name, delisted_at），由
    ``scripts/build_delisted_list.py`` 生成；文件缺失时本模块返回空清单
    （不阻断既有流程，但会告警一次）。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
from loguru import logger

from quart.config import data_root

DELISTED_PATH = data_root() / "meta" / "delisted.parquet"
DELISTED_COLUMNS = ["code", "name", "delisted_at"]


@lru_cache(maxsize=1)
def load_delisted(path: str | Path | None = None) -> pd.DataFrame:
    """读取退市清单（code, name, delisted_at）。

    文件缺失/损坏时返回空 DataFrame 并告警一次，调用方应把"无退市信息"
    视为数据风险而不是阻断信号。
    """
    p = Path(path) if path is not None else DELISTED_PATH
    if not p.exists():
        logger.warning(
            "delisted list missing at {}; 退市过滤不生效（数据层风险，"
            "请运行 scripts/build_delisted_list.py）", p,
        )
        return pd.DataFrame(columns=DELISTED_COLUMNS)
    try:
        df = pd.read_parquet(p)
    except Exception as exc:
        logger.warning("delisted list unreadable {}: {}", p, exc)
        return pd.DataFrame(columns=DELISTED_COLUMNS)
    if df.empty:
        return pd.DataFrame(columns=DELISTED_COLUMNS)
    missing = [c for c in DELISTED_COLUMNS if c not in df.columns]
    if missing:
        logger.warning("delisted list {} missing columns {}", p, missing)
        return pd.DataFrame(columns=DELISTED_COLUMNS)
    df = df[DELISTED_COLUMNS].copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["delisted_at"] = pd.to_datetime(df["delisted_at"], errors="coerce")
    return df.dropna(subset=["code", "delisted_at"])


def delisted_map(path: str | Path | None = None) -> dict[str, pd.Timestamp]:
    """返回 {code(6位): delisted_at}。"""
    df = load_delisted(path)
    if df.empty:
        return {}
    return {
        code: pd.Timestamp(ts)
        for code, ts in zip(df["code"], df["delisted_at"], strict=False)
    }


def filter_delisted_bars(
    bars: pd.DataFrame,
    delisted: dict[str, pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """剔除退市日（含）之后的 bar，保留退市前历史（避免幸存者偏差）。

    Parameters
    ----------
    bars:
        长表行情（date, symbol, ...）。
    delisted:
        {code: delisted_at}；None 时自动从 ``load_delisted()`` 读取。

    Returns
    -------
    过滤后的 bars 副本。
    """
    if bars is None or bars.empty:
        return bars
    mapping = delisted_map() if delisted is None else (delisted or {})
    if not mapping:
        return bars
    symbol = bars["symbol"].astype(str).str.zfill(6)
    dates = pd.to_datetime(bars["date"])
    limit = symbol.map(mapping)
    keep = limit.isna() | (dates < limit)
    dropped = int((~keep).sum())
    if dropped:
        logger.info("delisted-filter dropped {} bars after delisting date", dropped)
    return bars[keep].copy()


__all__ = [
    "DELISTED_PATH",
    "load_delisted",
    "delisted_map",
    "filter_delisted_bars",
]
