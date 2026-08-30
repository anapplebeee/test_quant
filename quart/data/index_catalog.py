"""A 股常用指数清单与板块分类。

用于：① `scripts/update_indices.py` 批量拉取指数日线；
② 数据总览页按板块展示指数覆盖情况（上证/深证/中证/科创/沪深300 等）。
"""
from __future__ import annotations

#: code / name / board（板块分类）
INDEX_CATALOG: list[dict[str, str]] = [
    {"code": "000001", "name": "上证指数", "board": "上证"},
    {"code": "000016", "name": "上证50", "board": "上证"},
    {"code": "000010", "name": "上证180", "board": "上证"},
    {"code": "000300", "name": "沪深300", "board": "沪深300"},
    {"code": "000905", "name": "中证500", "board": "中证"},
    {"code": "000852", "name": "中证1000", "board": "中证"},
    {"code": "000688", "name": "科创50", "board": "科创"},
    {"code": "399001", "name": "深证成指", "board": "深证"},
    {"code": "399006", "name": "创业板指", "board": "创业板"},
]

BOARDS: list[str] = ["上证", "沪深300", "中证", "科创", "深证", "创业板"]


def index_name(code: str) -> str:
    """代码 → 名称；未收录时返回代码本身。"""
    code = str(code).removeprefix("IDX")
    for item in INDEX_CATALOG:
        if item["code"] == code:
            return item["name"]
    return code


__all__ = ["BOARDS", "INDEX_CATALOG", "index_name"]
