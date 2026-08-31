"""从合同路由表与 DTO 生成 OpenAPI 3 规范（API-001）。

生成结果冻结为 `api/control/openapi_v1.json`；合同测试断言两者一致。
任何删除路由/字段、改名或改类型的变更都会打破一致性，迫使变更者
显式更新冻结文件——这就是 breaking-change 检查。
"""
from __future__ import annotations

import dataclasses
import json
import types
import typing
from pathlib import Path
from typing import Any

from api.control.dto import API_VERSION, CONTRACT_DTOS
from api.control.router import ROUTES

OPENAPI_PATH = Path(__file__).parent / "openapi_v1.json"

_JSON_PRIMITIVES = {
    str: "string",
    int: "integer",
    bool: "boolean",
    float: "number",
}


def _json_schema(annotation: Any) -> dict[str, Any]:
    """把 DTO 字段的类型注解映射为 JSON Schema 片段。"""
    if annotation in _JSON_PRIMITIVES:
        return {"type": _JSON_PRIMITIVES[annotation]}
    origin = typing.get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        schema = _json_schema(inner[0]) if len(inner) == 1 else {"type": "object"}
        return {**schema, "nullable": True}
    if origin is list:
        (item,) = typing.get_args(annotation)
        return {"type": "array", "items": _json_schema(item)}
    if origin is dict or annotation is dict:
        return {"type": "object"}
    return {"type": "object"}


def _dto_schema(dto_type: type) -> dict[str, Any]:
    hints = typing.get_type_hints(dto_type)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(dto_type):
        properties[f.name] = _json_schema(hints[f.name])
        if (
            f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
        ):
            required.append(f.name)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def generate_openapi() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for route in ROUTES:
        openapi_path = route.pattern
        params = [
            {"name": seg.strip("{}"), "in": "path", "required": True,
             "schema": {"type": "string"}}
            for seg in route.pattern.split("/")
            if seg.startswith("{")
        ]
        dto_name = _response_dto_of(route.handler)
        success_status = "201" if route.method == "POST" else "200"
        operation: dict[str, Any] = {
            "operationId": route.handler,
            "parameters": params,
            "responses": {
                success_status: {
                    "description": "成功",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{dto_name}"}
                        }
                    },
                },
                "default": {
                    "description": "错误",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorDTO"}
                        }
                    },
                },
            },
        }
        if route.method == "POST":
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
            if route.idempotency_required:
                operation["parameters"] = [
                    *operation["parameters"],
                    {"name": "Idempotency-Key", "in": "header", "required": True,
                     "schema": {"type": "string"}},
                ]
        paths.setdefault(openapi_path, {})[route.method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Quart Control API",
            "version": API_VERSION,
            "description": "版本化控制面合同（TARGET_ARCHITECTURE_V3 §7.1）",
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": {
                dto.__name__: _dto_schema(dto) for dto in CONTRACT_DTOS
            }
        },
    }


def _response_dto_of(handler: str) -> str:
    mapping = {
        "data_health": "HealthDTO",
        "submit_job": "JobDTO",
        "get_job": "JobDTO",
        "job_events": "JobEventsDTO",
        "get_artifact": "ArtifactRunDTO",
        "approve_trade_plan": "TradePlanDTO",
        "get_positions": "PositionsDTO",
        "create_order": "ErrorDTO",      # 合同占位：等待 BROKER-001 实现
        "cancel_order": "ErrorDTO",      # 合同占位：等待 BROKER-001 实现
        "create_reconciliation": "ErrorDTO",  # 合同占位：等待对账流程实现
    }
    return mapping[handler]


def write_openapi(path: Path | None = None) -> Path:
    target = path or OPENAPI_PATH
    target.write_text(
        json.dumps(generate_openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = ["OPENAPI_PATH", "generate_openapi", "write_openapi"]
