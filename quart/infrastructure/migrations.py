"""平台 migration 汇总（RISK-001、OMS-001）。

`PRAGMA user_version` 是全库共享的单一版本计数器：若各模块只应用自己的
migration 列表，先初始化的模块会把版本号推高，导致后初始化的模块低版本
migration 被跳过（表缺失）。因此所有仓储初始化时统一应用平台全量列表。
"""
from __future__ import annotations

from quart.infrastructure.db import Migration
from quart.infrastructure.job_schema import JOB_MIGRATIONS
from quart.oms.oms_schema import OMS_MIGRATIONS
from quart.risk.risk_schema import RISK_MIGRATIONS

#: 平台全部 migration（新增模块的 migration 在此并入）
PLATFORM_MIGRATIONS: list[Migration] = sorted(
    [*JOB_MIGRATIONS, *RISK_MIGRATIONS, *OMS_MIGRATIONS], key=lambda m: m.version
)

__all__ = ["PLATFORM_MIGRATIONS"]
