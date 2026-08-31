"""SQLite 连接与 Migration 框架（ADR-0001，DB-001）。

设计要点
--------
- **versioned migration**：用 `PRAGMA user_version` 记录 schema 版本，
  只前向应用未执行的 migration；每个 migration 是一个 `(version, name, up, down)`。
- **WAL 模式**：`PRAGMA journal_mode=WAL`，读写并发不互相阻塞。
- **busy timeout**：`PRAGMA busy_timeout`，并发写等待而非立即失败。
- **foreign_keys**：`PRAGMA foreign_keys=ON`，保证引用完整性。
- **幂等**：重复运行 `migrate()` 无副作用；`user_version` 已达目标则跳过。
- **回滚**：`down` 定义向下迁移，供回滚演练（协调文档批次 1 出口门槛）。

为什么用 `user_version` 而非 migration 表
-----------------------------------------
`PRAGMA user_version` 是 SQLite 内置的 schema 版本号，单值、原子、与事务
天然一致。比单独建 `_migrations` 表更简单，且无并发插入冲突风险。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from quart.config import PROJECT_ROOT

#: 默认数据库路径（平台基础设施库，与 manual_trading 账本分开）
DEFAULT_DB_PATH = PROJECT_ROOT / "state" / "quart.db"

#: 默认 busy timeout（毫秒）
BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class Migration:
    """一次 schema 迁移。"""

    version: int                      # 目标版本号（> 当前即应用）
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None] | None = None  # 向下迁移（可回滚）


class Database:
    """SQLite 连接管理 + migration 应用。

    每个实例对应一个数据库文件。连接按需创建（调用方负责 close），
    通过 `connect()` 提供配置好 pragma 的连接。
    """

    def __init__(self, path: Path | str | None = None, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.busy_timeout_ms = busy_timeout_ms
        # 单进程内 migration 互斥（SQLite 写锁本身也保证，此处防重入）
        self._migration_lock = threading.Lock()
        # WAL 模式只需初始化一次（并发下避免反复切换持锁）
        self._wal_initialized = False

    def connect(self) -> sqlite3.Connection:
        """创建并配置一个连接（WAL + busy timeout + foreign keys）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return conn

    def _enable_wal(self) -> None:
        """开启 WAL 模式（幂等）。journal_mode 是持久化设置，只需设一次。

        用实例标志避免并发下每次都执行 WAL 切换（WAL 切换可能持锁，
        导致并发 apply 偶发 'database is locked'）。
        """
        if self._wal_initialized:
            return
        with self._migration_lock:
            if self._wal_initialized:
                return
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
            self._wal_initialized = True

    # ---------------- Migration ----------------

    def current_version(self) -> int:
        """当前 schema 版本。"""
        with self.connect() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def apply(self, migrations: list[Migration]) -> list[int]:
        """前向应用所有未执行的 migration，返回实际应用的版本列表。

        每个 migration 在一个事务内应用：失败则回滚该 migration，
        保证不会留下半迁移状态。应用成功后更新 `user_version`。
        """
        self._enable_wal()
        applied: list[int] = []
        with self._migration_lock:
            # current_version 必须在锁内读取，否则并发线程会读到同一旧版本
            # 而重复应用同一 migration（如 ALTER TABLE 报 duplicate column）。
            current = self.current_version()
            for m in sorted(migrations, key=lambda m: m.version):
                if m.version <= current:
                    continue
                with self.connect() as conn:
                    try:
                        m.up(conn)
                        conn.execute(f"PRAGMA user_version = {m.version}")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                applied.append(m.version)
        return applied

    def rollback(self, migrations: list[Migration], to_version: int) -> list[int]:
        """向下回滚到指定版本（需 migration 定义 down）。返回回滚的版本列表。"""
        self._enable_wal()
        rolled: list[int] = []
        with self._migration_lock:
            current = self.current_version()
            for m in sorted(migrations, key=lambda m: m.version, reverse=True):
                if m.version <= to_version or m.version > current:
                    continue
                if m.down is None:
                    raise RuntimeError(
                        f"migration v{m.version}（{m.name}）未定义 down，无法回滚"
                    )
                with self.connect() as conn:
                    try:
                        m.down(conn)
                        conn.execute(f"PRAGMA user_version = {m.version - 1}")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                rolled.append(m.version)
        return rolled


#: 模块级单例（平台默认库）
_DEFAULT_DB: Database | None = None


def get_db() -> Database:
    """获取默认数据库单例。测试可传独立 path 绕过。"""
    global _DEFAULT_DB
    if _DEFAULT_DB is None:
        _DEFAULT_DB = Database()
    return _DEFAULT_DB


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_DB_PATH",
    "Database",
    "Migration",
    "get_db",
]
