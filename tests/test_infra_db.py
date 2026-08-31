"""DB-001：SQLite migration 框架测试。

验收（ADR-0001）：migration 可前向升级、可回滚演练、并发安全。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from quart.infrastructure.db import Database, Migration


def _migrations() -> list[Migration]:
    """构造两版迁移用于测试。"""
    def up_v1(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY, v TEXT)")

    def down_v1(conn):
        conn.execute("DROP TABLE IF EXISTS t1")

    def up_v2(conn):
        conn.execute("ALTER TABLE t1 ADD COLUMN extra TEXT")

    def down_v2(conn):
        conn.execute("ALTER TABLE t1 DROP COLUMN extra")

    return [
        Migration(1, "t1", up_v1, down_v1),
        Migration(2, "t1_extra", up_v2, down_v2),
    ]


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


# ---------------------------------------------------------------- 基础


def test_connect_sets_pragmas(db):
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    finally:
        conn.close()


def test_wal_mode_enabled(db):
    db._enable_wal()
    conn = db.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_initial_version_zero(db):
    assert db.current_version() == 0


# ---------------------------------------------------------------- 前向升级


def test_apply_runs_pending_migrations(db):
    applied = db.apply(_migrations())
    assert applied == [1, 2]
    assert db.current_version() == 2


def test_apply_is_idempotent(db):
    db.apply(_migrations())
    # 再次 apply：已执行的版本跳过
    assert db.apply(_migrations()) == []
    assert db.current_version() == 2


def test_apply_creates_tables(db):
    db.apply(_migrations())
    conn = db.connect()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" in tables
        # t1 应有 v2 加的 extra 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t1)")}
        assert "extra" in cols
    finally:
        conn.close()


def test_migration_failure_rolls_back(db):
    """v2 失败时不应留下半迁移状态，user_version 停在 v1。"""
    def bad_v2(conn):
        conn.execute("THIS IS NOT VALID SQL")

    ms = _migrations() + [Migration(3, "bad", bad_v2)]
    db.apply(_migrations())
    with pytest.raises(sqlite3.Error):
        db.apply(ms)
    assert db.current_version() == 2, "失败迁移不应推进 user_version"


# ---------------------------------------------------------------- 回滚


def test_rollback_to_version(db):
    db.apply(_migrations())
    rolled = db.rollback(_migrations(), to_version=0)
    assert rolled == [2, 1]
    assert db.current_version() == 0
    # 表应被删
    conn = db.connect()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" not in tables
    finally:
        conn.close()


def test_rollback_requires_down(db):
    """未定义 down 的 migration 无法回滚。"""
    def up(conn):
        pass

    ms = [Migration(1, "no_down", up, None)]
    db.apply(ms)
    with pytest.raises(RuntimeError, match="未定义 down"):
        db.rollback(ms, to_version=0)


def test_rollback_partial(db):
    db.apply(_migrations())
    rolled = db.rollback(_migrations(), to_version=1)
    assert rolled == [2]
    assert db.current_version() == 1
    conn = db.connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t1)")}
        assert "extra" not in cols, "v2 的 extra 列应被回滚"
    finally:
        conn.close()


# ---------------------------------------------------------------- 并发


def test_concurrent_apply_is_safe(db):
    """多线程同时 apply 同一批 migration 不应出错（幂等 + 锁）。"""
    errors = []

    def worker():
        try:
            db.apply(_migrations())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.current_version() == 2


def test_isolated_databases(tmp_path):
    """不同库互不影响。"""
    db1 = Database(tmp_path / "a.db")
    db2 = Database(tmp_path / "b.db")
    db1.apply(_migrations())
    assert db2.current_version() == 0
