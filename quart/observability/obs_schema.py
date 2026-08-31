"""可观测性指标表 schema（OBS-001，v4 migration）。"""
from __future__ import annotations

from quart.infrastructure.db import Migration


def _up_v4(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS obs_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            labels_json TEXT NOT NULL DEFAULT '{}',
            value REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_obs_metrics_name ON obs_metrics(name, created_at);
        """
    )


def _down_v4(conn) -> None:
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_obs_metrics_name;
        DROP TABLE IF EXISTS obs_metrics;
        """
    )


OBS_MIGRATIONS: list[Migration] = [
    Migration(version=4, name="obs_metrics", up=_up_v4, down=_down_v4),
]

__all__ = ["OBS_MIGRATIONS"]
