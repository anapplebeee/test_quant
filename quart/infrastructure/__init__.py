"""基础设施层：数据库连接、Migration、持久化 Job。

主工作流：F 平台与质量（后端平台工程）。对应 ADR-0001。
本目录只放**通用平台骨架**，不含 ARCH-001 待冻结的订单/风控领域字段。
"""
from __future__ import annotations

from quart.infrastructure.db import Database, get_db, DEFAULT_DB_PATH

__all__ = ["DEFAULT_DB_PATH", "Database", "get_db"]
