"""结构化日志（OBS-001，TARGET_ARCHITECTURE_V3 §13.2）。

结构化日志字段至少包含：``trace_id, job_id, run_id, account_id,
plan_id, order_id, broker_order_id, strategy, environment``。

实现基于 loguru：

- `log_event` 输出"事件 + 关联字段"的结构化日志（JSON 序列化后每行一条）；
- `trace_context` 把关联字段绑定到当前上下文，块内全部日志自动携带，
  使单笔 job / order / reconcile 可以按字段检索出全链路；
- `configure_structured_logging` 追加 JSONL 文件 sink（``serialize=True``），
  与 stderr 人类可读输出并存。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from quart.domain import new_id

#: 架构 §13.2 要求的标准关联字段
TRACE_FIELDS: tuple[str, ...] = (
    "trace_id",
    "job_id",
    "run_id",
    "account_id",
    "plan_id",
    "order_id",
    "broker_order_id",
    "strategy",
    "environment",
)


def new_trace_id() -> str:
    """生成一次调用链的追踪 ID。"""
    return new_id("trace")


@contextmanager
def trace_context(**fields: Any):
    """把关联字段绑定到当前上下文，块内所有日志自动携带。

    ``None`` 值字段会被忽略，避免污染日志检索。
    """
    bound = {k: v for k, v in fields.items() if v is not None}
    with logger.contextualize(**bound):
        yield


def log_event(event: str, *, level: str = "INFO", **fields: Any) -> None:
    """输出一条结构化事件日志。

    ``event`` 是稳定的事件名（如 ``order.transition``、``job.finished``），
    其余为关联字段；序列化后可按 ``event`` 与任意字段检索全链路。
    """
    clean = {k: v for k, v in fields.items() if v is not None}
    logger.bind(event=event, **clean).log(level, event)


def configure_structured_logging(
    log_path: Path | str | None = None,
    level: str = "INFO",
) -> list[int]:
    """启用 JSONL 结构化文件输出，返回新增 sink id（供移除）。

    ``log_path`` 为 None 时不新增 sink（仅依赖既有控制台输出）。
    """
    if log_path is None:
        return []
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sink_id = logger.add(str(path), level=level, serialize=True, encoding="utf-8")
    return [sink_id]


__all__ = [
    "TRACE_FIELDS",
    "configure_structured_logging",
    "log_event",
    "new_trace_id",
    "trace_context",
]
