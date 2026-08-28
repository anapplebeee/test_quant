"""共享工具函数"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

# ---- 输入白名单（防路径穿越：API/UI 传入的代码、日期、任务名必须先过这里） ----
_SYMBOL_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{8}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def valid_symbol(symbol: str) -> bool:
    """股票代码：6 位数字（含全部 A 股板块前缀）"""
    return bool(_SYMBOL_RE.match(str(symbol)))


def valid_date8(date: str) -> bool:
    """日期：YYYYMMDD 8 位数字"""
    return bool(_DATE_RE.match(str(date)))


def valid_name(name: str) -> bool:
    """任务/策略名：仅字母数字下划线"""
    return bool(_NAME_RE.match(str(name)))


def safe_path(base: str | os.PathLike, *parts: str) -> Path | None:
    """拼接路径并确认结果仍在 base 目录内，防穿越；非法返回 None"""
    base_p = Path(base).resolve()
    try:
        target = (base_p.joinpath(*parts)).resolve()
    except (OSError, ValueError):
        return None
    if base_p != target and base_p not in target.parents:
        return None
    return target


def load_stock_names() -> dict[str, str]:
    """获取股票代码-名称映射，优先读缓存"""
    cache_path = os.path.join("data", "stock_names.parquet")
    if os.path.exists(cache_path):
        df = pd.read_parquet(cache_path)
        return dict(zip(df["code"], df["name"]))
    # 缓存不存在则用 akshare 拉取
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        df.to_parquet(cache_path, index=False)
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}
