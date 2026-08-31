"""全局 ID 与幂等键工具。"""
from __future__ import annotations

import re
from typing import NewType
from uuid import NAMESPACE_URL, uuid4, uuid5

AccountId = NewType("AccountId", str)
IntentId = NewType("IntentId", str)
DecisionId = NewType("DecisionId", str)
ClientOrderId = NewType("ClientOrderId", str)
BrokerOrderId = NewType("BrokerOrderId", str)
EventId = NewType("EventId", str)
FillId = NewType("FillId", str)
IdempotencyKey = NewType("IdempotencyKey", str)

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def require_id(value: object, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    return text


def new_id(prefix: str) -> str:
    """生成可跨进程使用的随机业务 ID。"""
    _validate_prefix(prefix)
    return f"{prefix}_{uuid4().hex}"


def stable_id(prefix: str, key: object) -> str:
    """由上游唯一键生成稳定 ID，用于安全重试和兼容转换。"""
    _validate_prefix(prefix)
    key_text = require_id(key, "稳定 ID 的 key")
    return f"{prefix}_{uuid5(NAMESPACE_URL, f'quart:{prefix}:{key_text}').hex}"


def _validate_prefix(prefix: str) -> None:
    if not _PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"非法 ID 前缀: {prefix}")


__all__ = [
    "AccountId",
    "BrokerOrderId",
    "ClientOrderId",
    "DecisionId",
    "EventId",
    "FillId",
    "IdempotencyKey",
    "IntentId",
    "new_id",
    "require_id",
    "stable_id",
]
