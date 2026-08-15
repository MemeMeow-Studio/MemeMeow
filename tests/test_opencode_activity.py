"""OpenCode SQLite 活跃度适配器的只读、批量和降级行为测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.opencode_activity import AgentActivity, OpenCodeActivityReader


def _create_database(path: Path) -> sqlite3.Connection:
    """创建只包含适配器边界所需列的 OpenCode 测试数据库。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            time_created INTEGER NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def _insert_session(connection: sqlite3.Connection, session_id: str, task_id: str, created: int, parts: list[tuple[int, str]]) -> None:
    """插入指定任务 session 及其最小事件元数据。"""
    connection.execute("INSERT INTO session(id, title, time_created) VALUES (?, ?, ?)", (session_id, f"mememeow-task-{task_id}", created))
    connection.executemany(
        "INSERT INTO part(id, session_id, time_updated, data) VALUES (?, ?, ?, ?)",
        [(f"{session_id}-{index}", session_id, updated, json.dumps({"type": event_type, "text": "不应被返回"})) for index, (updated, event_type) in enumerate(parts)],
    )


def test_reader_counts_steps_and_converts_milliseconds_to_utc(tmp_path: Path):
    """读取完成和进行中轮次，并把最新 part 时间转换为 UTC。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    connection = _create_database(runtime / "opencode.db")
    task_id = "task-1"
    _insert_session(connection, "session-1", task_id, 10, [(1000, "step-start"), (2000, "step-finish"), (3000, "step-start")])
    connection.commit()
    connection.close()

    result = OpenCodeActivityReader(runtime).read_many([task_id])

    assert result[task_id] == AgentActivity(1, True, "1970-01-01T00:00:03Z")


def test_reader_uses_newest_duplicate_session(tmp_path: Path):
    """同一标题存在多次尝试时只统计创建时间最新的 session。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    connection = _create_database(runtime / "opencode.db")
    _insert_session(connection, "old", "same", 10, [(1000, "step-finish"), (1100, "step-finish")])
    _insert_session(connection, "new", "same", 20, [(2000, "step-start")])
    connection.commit()
    connection.close()

    result = OpenCodeActivityReader(runtime).read_many(["same"])

    assert result["same"].completed_turns == 0
    assert result["same"].turn_running is True
    assert result["same"].last_activity_at == "1970-01-01T00:00:02Z"


def test_reader_empty_input_and_missing_session_do_not_open_or_fabricate(tmp_path: Path):
    """空输入和历史任务缺少 session 时都返回空映射。"""
    reader = OpenCodeActivityReader(tmp_path / "missing")
    assert reader.read_many([]) == {}
    assert reader.read_many(["missing-task"]) == {}


def test_reader_sees_uncheckpointed_wal_rows(tmp_path: Path):
    """保持 writer 打开时，原目录只读连接仍能看到 WAL 中已提交的行。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "opencode.db"
    connection = _create_database(path)
    connection.execute("PRAGMA journal_mode=WAL")
    _insert_session(connection, "wal-session", "wal-task", 10, [(1234, "step-finish")])
    connection.commit()
    assert (runtime / "opencode.db-wal").exists()

    result = OpenCodeActivityReader(runtime).read_many(["wal-task"])

    assert result["wal-task"].completed_turns == 1
    connection.close()


@pytest.mark.parametrize("failure", ["permission", "schema", "time"])
def test_reader_failure_paths_degrade_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str):
    """权限、schema 和时间异常均不影响调用方并且不返回部分活动字段。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "opencode.db"
    if failure == "schema":
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE session(id TEXT)")
        connection.commit()
        connection.close()
    else:
        connection = _create_database(path)
        _insert_session(connection, "bad-session", "bad-task", 10, [("bad-time", "step-finish")]) if failure == "time" else _insert_session(connection, "bad-session", "bad-task", 10, [(1000, "step-finish")])
        if failure == "time":
            connection.execute("UPDATE part SET time_updated = 'bad-time'")
        connection.commit()
        connection.close()
    reader = OpenCodeActivityReader(runtime)
    if failure == "permission":
        monkeypatch.setattr(reader, "_connect", lambda: (_ for _ in ()).throw(PermissionError("denied")))

    assert reader.read_many(["bad-task"]) == {}


def test_reader_malformed_json_and_locked_database_degrade(tmp_path: Path):
    """坏 JSON 与短暂排他锁都收敛为空，不抛出数据库异常。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "opencode.db"
    connection = _create_database(path)
    connection.execute("INSERT INTO session(id, title, time_created) VALUES ('json', 'mememeow-task-json', 1)")
    connection.execute("INSERT INTO part(id, session_id, time_updated, data) VALUES ('json-part', 'json', 1, '{bad')")
    connection.commit()
    connection.close()
    assert OpenCodeActivityReader(runtime).read_many(["json"]) == {}

    writer = sqlite3.connect(path, timeout=1)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        assert OpenCodeActivityReader(runtime, busy_timeout_ms=1).read_many(["json"]) == {}
    finally:
        writer.rollback()
        writer.close()


def test_reader_uses_read_only_mode_without_copying_or_special_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """连接参数必须使用 mode=ro，并启用 query_only 而不改写 runtime。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    connection = _create_database(runtime / "opencode.db")
    connection.commit()
    connection.close()
    real_connect = sqlite3.connect
    calls: list[tuple[object, ...]] = []

    def capture(*args, **kwargs):
        """记录连接参数后交给标准库完成真实只读连接。"""
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", capture)
    OpenCodeActivityReader(runtime).read_many(["none"])

    uri = str(calls[0][0][0])
    assert "mode=ro" in uri
    assert "immutable" not in uri
