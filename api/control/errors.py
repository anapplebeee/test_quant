"""Control API 错误语义（API-001，TARGET_ARCHITECTURE_V3 §7.1）。

路由层只返回结构化错误 `{code, message, details}`，不抛裸异常给调用方。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ApiErrorCode:
    """稳定错误码（合同的一部分，改名/删除属于 breaking change）。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


_STATUS_BY_CODE = {
    ApiErrorCode.VALIDATION_ERROR: 400,
    ApiErrorCode.NOT_FOUND: 404,
    ApiErrorCode.CONFLICT: 409,
    ApiErrorCode.METHOD_NOT_ALLOWED: 405,
    ApiErrorCode.SERVICE_UNAVAILABLE: 503,
    ApiErrorCode.INTERNAL: 500,
}


@dataclass(frozen=True, slots=True)
class ApiError(Exception):
    """Control API 域内异常；路由层捕获并映射为错误响应。"""

    code: str
    message: str
    status: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", self.status or _STATUS_BY_CODE.get(self.code, 500))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


__all__ = ["ApiError", "ApiErrorCode"]
