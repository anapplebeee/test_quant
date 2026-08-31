"""可观测性（OBS-001）：结构化日志 + 核心指标。"""
from quart.observability.metrics import MetricsRepository, collect_core_metrics
from quart.observability.obs_schema import OBS_MIGRATIONS
from quart.observability.structured import (
    TRACE_FIELDS,
    configure_structured_logging,
    log_event,
    new_trace_id,
    trace_context,
)

__all__ = [
    "OBS_MIGRATIONS",
    "TRACE_FIELDS",
    "MetricsRepository",
    "collect_core_metrics",
    "configure_structured_logging",
    "log_event",
    "new_trace_id",
    "trace_context",
]
