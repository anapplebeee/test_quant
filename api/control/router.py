"""Control API v1 同进程路由器（API-001，TARGET_ARCHITECTURE_V3 §7.1）。

Gradio 先作为同进程客户端调用 `ControlRouter.dispatch(method, path, ...)`；
未来替换为独立 SPA/HTTP 服务时，只需把 HTTP 层映射到同一入口，
合同（路由表 + DTO + Idempotency-Key 语义）不变。

路由层职责边界：只做参数校验、幂等键检查、调用 application service、
映射响应。业务逻辑一律在 `ControlServiceV1`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from api.control.dto import API_VERSION
from api.control.errors import ApiError, ApiErrorCode

_PREFIX = f"/api/{API_VERSION}"


class _Handler(Protocol):
    def __call__(
        self,
        params: dict[str, str],
        body: dict[str, Any],
        idempotency_key: str | None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: str
    handler: str
    idempotency_required: bool = False


#: 合同路由表（增删路由会改变 OpenAPI 合同，受合同测试守护）
ROUTES: tuple[Route, ...] = (
    Route("GET", f"{_PREFIX}/data/health", "data_health"),
    Route("POST", f"{_PREFIX}/jobs", "submit_job", idempotency_required=True),
    Route("GET", f"{_PREFIX}/jobs/{{job_id}}", "get_job"),
    Route("GET", f"{_PREFIX}/jobs/{{job_id}}/events", "job_events"),
    Route("GET", f"{_PREFIX}/artifacts/{{run_id}}", "get_artifact"),
    Route(
        "POST",
        f"{_PREFIX}/trade-plans/{{plan_id}}/approve",
        "approve_trade_plan",
        idempotency_required=True,
    ),
    Route("POST", f"{_PREFIX}/orders", "create_order", idempotency_required=True),
    Route(
        "POST",
        f"{_PREFIX}/orders/{{order_id}}/cancel",
        "cancel_order",
        idempotency_required=True,
    ),
    Route("GET", f"{_PREFIX}/accounts/{{account_id}}/positions", "get_positions"),
    Route(
        "POST",
        f"{_PREFIX}/reconciliations",
        "create_reconciliation",
        idempotency_required=True,
    ),
)

_SEGMENT_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _match(pattern: str, path: str) -> dict[str, str] | None:
    pattern_parts = [p for p in pattern.split("/") if p]
    path_parts = [p for p in path.split("/") if p]
    if len(pattern_parts) != len(path_parts):
        return None
    params: dict[str, str] = {}
    for pat, part in zip(pattern_parts, path_parts, strict=True):
        m = _SEGMENT_RE.match(pat)
        if m:
            if not part:
                return None
            params[m.group(1)] = part
        elif pat != part:
            return None
    return params


class ControlRouter:
    """把 (method, path, body, headers) 分发到 ControlServiceV1。"""

    def __init__(self, service: object):
        self.service = service

    def dispatch(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """统一响应信封：`{"status", "data"}` 或 `{"status", "error"}`。"""
        method = method.upper()
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        matched_params: dict[str, str] | None = None
        matched_route: Route | None = None
        path_matched = False
        for route in ROUTES:
            params = _match(route.pattern, path)
            if params is None:
                continue
            path_matched = True
            if route.method == method:
                matched_route = route
                matched_params = params
                break
        if matched_route is None:
            err = (
                ApiError(ApiErrorCode.METHOD_NOT_ALLOWED, f"路径 {path} 不支持 {method}")
                if path_matched
                else ApiError(ApiErrorCode.NOT_FOUND, f"未知路径: {path}")
            )
            return {"status": err.status, "error": err.to_dict()}

        idempotency_key: str | None = lowered.get("idempotency-key") or None
        if matched_route.idempotency_required and not idempotency_key:
            err = ApiError(
                ApiErrorCode.VALIDATION_ERROR,
                "mutation 请求必须携带 Idempotency-Key 请求头",
            )
            return {"status": err.status, "error": err.to_dict()}

        handler: _Handler = getattr(self.service, matched_route.handler)
        try:
            result = handler(matched_params or {}, body or {}, idempotency_key)
        except ApiError as exc:
            return {"status": exc.status, "error": exc.to_dict()}
        except Exception as exc:  # 路由层兜底，绝不把裸异常抛给调用方
            err = ApiError(ApiErrorCode.INTERNAL, f"内部错误: {exc}")
            return {"status": err.status, "error": err.to_dict()}

        if isinstance(result, tuple):
            dto, status = result
        else:
            dto, status = result, 200
        return {"status": status, "data": dto.to_dict()}


__all__ = ["ROUTES", "ControlRouter", "Route"]
