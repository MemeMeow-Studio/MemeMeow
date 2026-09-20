# 使用真实 SQLite 连接验证 Executor 的主 session 金额读取及错误分类。

from decimal import Decimal
import json
import os
import select

from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator

import pytest

from executor.analysis_usage import AnalysisUsageError, read_analysis_usage
from executor.analysis_monitor import AnalysisControlError, AnalysisMonitor


@pytest.fixture
def analysis_monitor_root() -> Iterator[Path]:
    """在仓库测试缓存目录创建一次性监控文件根目录。"""
    root = Path(".pytest_cache/analysis_usage_monitor")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)


def test_analysis_monitor_accepts_only_opencode_peer(analysis_monitor_root: Path) -> None:
    """状态 socket 只接受预先登记的 OpenCode 主进程，工具子进程的消息不会改变状态。"""
    socket_path = analysis_monitor_root / "analysis.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    monitor = AnalysisMonitor(
        attempt_id="attempt-analysis",
        policy={"version": 1, "termination_cost": "0.30"},
        database=analysis_monitor_root / "missing.db",
        directory=analysis_monitor_root,
        status_path=analysis_monitor_root / "unused.json",
        startup_deadline=0,
        status_socket=listener,
        expected_pid=os.getpid(),
    )
    child_code = (
        "import json,socket,sys; "
        "client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
        "client.connect(sys.argv[1]); "
        "client.sendall(json.dumps({'attempt_id':'attempt-analysis','plugin_version':'1.18.18','policy_version':1,'ready':True}).encode()); "
        "client.close()"
    )
    subprocess.run([sys.executable, "-c", child_code, str(socket_path)], check=True, start_new_session=True)
    readable, _, _ = select.select([listener], [], [], 1)
    assert readable

    assert monitor._read_socket_status() is None

    same_session = subprocess.Popen([
        sys.executable,
        "-c",
        child_code + "; import time; print('ready', flush=True); time.sleep(30)",
        str(socket_path),
    ], stdout=subprocess.PIPE)
    same_session.stdout.read(5)
    try:
        readable, _, _ = select.select([listener], [], [], 1)
        assert readable
        assert monitor._read_socket_status() == {
            "attempt_id": "attempt-analysis",
            "plugin_version": "1.18.18",
            "policy_version": 1,
            "ready": True,
        }
    finally:
        same_session.terminate()
        same_session.wait(timeout=2)

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.sendall(json.dumps({
        "attempt_id": "attempt-analysis",
        "plugin_version": "1.18.18",
        "policy_version": 1,
        "ready": True,
    }).encode())
    client.close()
    assert monitor._read_socket_status() == {
        "attempt_id": "attempt-analysis",
        "plugin_version": "1.18.18",
        "policy_version": 1,
        "ready": True,
    }
    listener.close()


@pytest.fixture
def database():
    """在项目测试目录创建独立 SQLite 数据，结束时释放连接和文件。"""
    root = Path(".pytest_cache/analysis_usage")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        path = Path(directory) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, cost REAL)")
        connection.executemany(
            "INSERT INTO session VALUES (?, ?, ?, ?)",
            [("main", None, "/workspace", 0.23), ("child", "main", "/workspace", 9),
             ("other", None, "/other", 8)],
        )
        connection.commit()
        connection.close()
        yield path


def test_bound_main_session_only(database):
    """读取指定主 session，忽略子 session 和其他目录的用量。"""
    usage = read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
    assert usage.observed_cost == Decimal("0.23")
    assert usage.checked_at.endswith("+00:00")


@pytest.mark.parametrize("session,reason", [
    ("missing", "session_missing"), ("child", "session_binding_mismatch"),
    ("other", "session_binding_mismatch"), ("", "session_binding_missing"),
])
def test_binding_errors(database, session, reason):
    """缺失绑定、子 session 和其他目录必须返回明确原因。"""
    with pytest.raises(AnalysisUsageError) as error:
        read_analysis_usage(database, session_id=session, directory=Path("/workspace"))
    assert error.value.code == "agent_analysis_usage_unavailable"
    assert error.value.reason == reason


def test_cost_cannot_decrease_on_resume(database):
    """恢复时金额减少不能被接受为新的用量起点。"""
    with pytest.raises(AnalysisUsageError, match="session_cost_decreased"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"), minimum_cost=Decimal("0.24"))


@pytest.mark.parametrize("value", [-1, float("inf"), "invalid", None])
def test_invalid_cost(database, value):
    """实际写入非法 SQLite 值，验证读取端不会将其当作零金额。"""
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE session SET cost = ? WHERE id = 'main'", (value,))
    with pytest.raises(AnalysisUsageError, match="session_cost_invalid"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"))


def test_missing_database_does_not_create_file(database):
    """只读打开失败不得创建空数据库。"""
    missing = database.with_name("missing.db")
    with pytest.raises(AnalysisUsageError, match="database_missing"):
        read_analysis_usage(missing, session_id="main", directory=Path("/workspace"))
    assert not missing.exists()


def test_schema_incompatible(database):
    """缺失控制字段必须识别为 schema 不兼容。"""
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE session DROP COLUMN cost")
    with pytest.raises(AnalysisUsageError, match="session_schema_incompatible"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"))


@pytest.mark.parametrize("cost", [0.19, 0.20, 0.23])
def test_monitor_reads_main_session_threshold(database, analysis_monitor_root: Path, cost: float) -> None:
    """监控器读取实际 SQLite 金额，达到边界时报告终止并保存观测值。"""
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE session SET cost = ? WHERE id = 'main'", (cost,))
    monitor = AnalysisMonitor(
        attempt_id="attempt-analysis", policy={"version": 1, "termination_cost": "0.20"},
        database=database, directory=Path("/workspace"),
        status_path=analysis_monitor_root / "pending.json", startup_deadline=float("inf"),
    )
    if cost >= 0.20:
        with pytest.raises(AnalysisControlError) as failure:
            monitor.check("main")
        assert failure.value.code == "agent_maximum_analysis_depth_exceeded"
        assert failure.value.reason == "termination_cost_reached"
    else:
        monitor.check("main")
    assert monitor.observed_cost == Decimal(str(cost))
    assert monitor.usage_checked_at is not None


@pytest.mark.parametrize("exited", [False, True])
def test_monitor_requires_session_before_deadline(analysis_monitor_root: Path, exited: bool) -> None:
    """session 尚未绑定时允许启动等待，期限届满或进程退出必须报告具体原因。"""
    monitor = AnalysisMonitor(
        attempt_id="attempt-analysis", policy={"version": 1, "termination_cost": "0.20"},
        database=analysis_monitor_root / "missing.db", directory=analysis_monitor_root,
        status_path=analysis_monitor_root / "pending.json", startup_deadline=float("inf"),
    )
    monitor.check(None)
    assert monitor.usage_checked_at is None
    monitor.next_check = 0
    if not exited:
        monitor.startup_deadline = 0
    with pytest.raises(AnalysisControlError) as failure:
        monitor.check(None, exited=exited)
    assert failure.value.code == "agent_analysis_usage_unavailable"
    assert failure.value.reason == "session_binding_deadline_exceeded"


def test_monitor_reports_missing_task_database(analysis_monitor_root: Path) -> None:
    """已绑定 session 的任务缺少数据库时立即返回用量错误。"""
    monitor = AnalysisMonitor(
        attempt_id="attempt-analysis", policy={"version": 1, "termination_cost": "0.20"},
        database=analysis_monitor_root / "missing.db", directory=analysis_monitor_root,
        status_path=analysis_monitor_root / "pending.json", startup_deadline=float("inf"),
    )
    with pytest.raises(AnalysisControlError) as failure:
        monitor.check("main")
    assert failure.value.code == "agent_analysis_usage_unavailable"
    assert failure.value.reason == "database_missing"


def test_usage_reads_committed_cost_during_wal_write(database) -> None:
    """WAL 写事务进行期间读取已提交金额，提交完成后能够读取新增金额。"""
    with sqlite3.connect(database) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE session SET cost = 0.31 WHERE id = 'main'")
        pending = read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
        assert pending.observed_cost == Decimal("0.23")
        writer.commit()
        committed = read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
        assert committed.observed_cost == Decimal("0.31")


def test_usage_reports_exclusive_database_lock(database) -> None:
    """数据库排他锁超过读取期限时保留 SQLite 错误类别。"""
    with sqlite3.connect(database) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(AnalysisUsageError, match="database_read_error:SQLITE_BUSY"):
            read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
