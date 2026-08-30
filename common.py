"""共享工具函数：输入白名单、路径解析、统一降级告警。

这里只放**跨层共用**的工具。业务规则请放回 quart/ 各自的子包，
避免这里退化成杂物抽屉。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

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


# ---- 统一路径解析（唯一来源，杜绝各处硬编码 "data" / "reports"） ----


def data_dir() -> Path:
    """数据仓库根目录。改 settings.yaml 的 data.root 后，全项目一致生效。"""
    from quart.config import data_root

    return data_root()


def reports_dir() -> Path:
    from quart.config import PROJECT_ROOT

    return PROJECT_ROOT / "reports"


def daily_dir() -> Path:
    return data_dir() / "daily"


def universe_dir() -> Path:
    return data_dir() / "universe"


def index_dir() -> Path:
    return data_dir() / "index"


# ---- 统一降级告警 ----


def degraded(where: str, exc: BaseException, logger: Any = None) -> None:
    """降级返回空数据可以，静默丢弃不行。

    api 层全部异常都必须经过这里留痕，否则前端"未找到数据"到底是真没有
    还是读取失败，永远查不出来。
    """
    msg = f"[{where}] degraded: {exc}"
    if logger is not None:
        logger.warning(msg)
        return
    try:
        from loguru import logger as _logger

        _logger.warning(msg)
    except ImportError:
        print(f"WARNING {msg}")


def load_stock_names() -> dict[str, str]:
    """获取股票代码-名称映射，优先读缓存"""
    cache_path = daily_dir().parent / "stock_names.parquet"
    if cache_path.exists():
        try:
            df = pd.read_parquet(cache_path)
            return dict(zip(df["code"], df["name"]))
        except Exception:
            pass
    # 缓存不存在则用 akshare 拉取
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path, index=False)
        return dict(zip(df["code"], df["name"]))
    except Exception as exc:
        degraded("load_stock_names", exc)
        return {}
