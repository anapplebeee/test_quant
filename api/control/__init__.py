"""Control API v1（API-001，TARGET_ARCHITECTURE_V3 §7.1）。

版本化控制面合同：同进程路由 + 稳定 DTO + Idempotency-Key 语义。
Gradio 前端作为同进程客户端调用 `ControlRouter.dispatch`；未来替换为
独立 HTTP 服务时，只需把 HTTP 层映射到同一入口，合同不变。

- `router.ROUTES` + `dto.CONTRACT_DTOS` 是合同本体；
- `openapi_v1.json` 是冻结的 OpenAPI 规范，由合同测试守护一致性；
- 业务逻辑集中在 `service.ControlServiceV1`，路由只做校验与映射。
"""
from api.control.dto import (
    API_VERSION,
    CONTRACT_DTOS,
    ArtifactRunDTO,
    ErrorDTO,
    HealthDTO,
    JobDTO,
    JobEventsDTO,
    TradePlanDTO,
)
from api.control.errors import ApiError, ApiErrorCode
from api.control.openapi import generate_openapi, write_openapi
from api.control.router import ROUTES, ControlRouter, Route
from api.control.service import ControlServiceV1

__all__ = [
    "API_VERSION",
    "CONTRACT_DTOS",
    "ROUTES",
    "ApiError",
    "ApiErrorCode",
    "ArtifactRunDTO",
    "ControlRouter",
    "ControlServiceV1",
    "ErrorDTO",
    "HealthDTO",
    "JobDTO",
    "JobEventsDTO",
    "Route",
    "TradePlanDTO",
    "generate_openapi",
    "write_openapi",
]
