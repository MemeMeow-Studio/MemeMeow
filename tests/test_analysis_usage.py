# 使用真实 SQLite 连接验证 Executor 的主 session 金额读取及错误分类。

from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import select

from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator

import pytest

from executor.analysis_usage import AnalysisUsageError, read_analysis_usage, read_broker_analysis_usage
from executor.analysis_monitor import AnalysisControlError, AnalysisMonitor


class _AnalysisUsageHandler(BaseHTTPRequestHandler):
    """提供可配置的本地 broker HTTP 协议端点。"""

    def do_GET(self) -> None:
        """记录请求头，并按测试配置返回状态、响应头和字节正文。"""
        self.server.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "attempt_id": self.headers.get("X-MemeMeow-Executor-Attempt-ID"),
        })
        status, headers, body = self.server.responses.get(self.path, (404, {}, b""))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        """测试服务不写入标准错误。"""


@pytest.fixture
def broker_server():
    """启动真实本地 HTTP 服务并在测试结束后完整关闭。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AnalysisUsageHandler)
    server.requests = []
    server.responses = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _broker_response(*, attempt_id: str = "attempt-analysis", cost: object = "0.12", checked_at: object = "2026-09-19T08:00:00+00:00") -> bytes:
    """生成 broker 用量协议的 JSON 字节正文。"""
    return json.dumps({
        "executor_attempt_id": attempt_id,
        "observed_cost": cost,
        "checked_at": checked_at,
    }).encode("utf-8")


def _broker_url(server: ThreadingHTTPServer) -> str:
    """返回当前本地服务的版本化 endpoint。"""
    return f"http://127.0.0.1:{server.server_port}/v1"


@pytest.fixture
def analysis_monitor_root() -> Iterator[Path]:
    """在仓库测试缓存目录创建一次性监控文件根目录。"""
    root = Path(".pytest_cache/analysis_usage_monitor")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)


def test_broker_usage_uses_bound_capability_and_attempt(broker_server) -> None:
    """broker 查询必须携带当前 capability 和 attempt，并返回可信金额快照。"""
    broker_server.responses["/v1/analysis-usage"] = (
        200,
        {"Content-Type": "application/json"},
        _broker_response(),
    )

    usage = read_broker_analysis_usage(
        _broker_url(broker_server),
        capability="capability-" + "x" * 20,
        attempt_id="attempt-analysis",
    )

    assert usage.observed_cost == Decimal("0.12")
    assert usage.checked_at == "2026-09-19T08:00:00+00:00"
    assert broker_server.requests == [{
        "path": "/v1/analysis-usage",
        "authorization": "Bearer capability-xxxxxxxxxxxxxxxxxxxx",
        "attempt_id": "attempt-analysis",
    }]


@pytest.mark.parametrize(
    ("body", "minimum_cost", "reason"),
    [
        (_broker_response(attempt_id="other-attempt"), Decimal("0"), "attempt_binding_mismatch"),
        (_broker_response(cost="0.11"), Decimal("0.12"), "session_cost_decreased"),
        (_broker_response(cost=True), Decimal("0"), "session_cost_invalid"),
        (_broker_response(cost="NaN"), Decimal("0"), "session_cost_invalid"),
        (_broker_response(cost="-0.01"), Decimal("0"), "session_cost_invalid"),
        (_broker_response(checked_at="2026-09-19T08:00:00"), Decimal("0"), "usage_checked_at_invalid"),
        (json.dumps({"executor_attempt_id": "attempt-analysis", "observed_cost": "0.12", "checked_at": "2026-09-19T08:00:00+00:00", "extra": True}).encode("utf-8"), Decimal("0"), "broker_usage_response_invalid"),
    ],
)
def test_broker_usage_rejects_untrusted_snapshots(broker_server, body: bytes, minimum_cost: Decimal, reason: str) -> None:
    """attempt 冲突、金额倒退、无时区时间和未知字段都不能成为终止事实。"""
    broker_server.responses["/v1/analysis-usage"] = (200, {"Content-Type": "application/json"}, body)

    with pytest.raises(AnalysisUsageError, match=reason):
        read_broker_analysis_usage(
            _broker_url(broker_server),
            capability="capability-" + "x" * 20,
            attempt_id="attempt-analysis",
            minimum_cost=minimum_cost,
        )


def test_broker_usage_rejects_redirect_without_forwarding_capability(broker_server) -> None:
    """broker 跳转必须被拒绝，capability 不能转发到 Location。"""
    broker_server.responses["/v1/analysis-usage"] = (302, {"Location": "/redirected"}, b"")
    broker_server.responses["/redirected"] = (200, {"Content-Type": "application/json"}, _broker_response())

    with pytest.raises(AnalysisUsageError, match="broker_usage_http_302"):
        read_broker_analysis_usage(
            _broker_url(broker_server),
            capability="capability-" + "x" * 20,
            attempt_id="attempt-analysis",
        )

    assert [request["path"] for request in broker_server.requests] == ["/v1/analysis-usage"]


def test_broker_usage_rejects_http_errors_and_oversized_responses(broker_server) -> None:
    """HTTP 故障和超过协议上限的正文必须保留明确原因。"""
    broker_server.responses["/v1/analysis-usage"] = (503, {"Content-Type": "application/json"}, b"{}")
    with pytest.raises(AnalysisUsageError, match="broker_usage_http_503"):
        read_broker_analysis_usage(
            _broker_url(broker_server),
            capability="capability-" + "x" * 20,
            attempt_id="attempt-analysis",
        )
    broker_server.responses["/v1/analysis-usage"] = (
        200,
        {"Content-Type": "application/json"},
        b"x" * (64 * 1024 + 1),
    )
    with pytest.raises(AnalysisUsageError, match="broker_usage_response_too_large"):
        read_broker_analysis_usage(
            _broker_url(broker_server),
            capability="capability-" + "x" * 20,
            attempt_id="attempt-analysis",
        )


def test_analysis_monitor_uses_broker_cost_without_local_database(broker_server, analysis_monitor_root: Path) -> None:
    """配置 broker 后，Executor 依据 attempt 金额终止且不依赖任务 SQLite。"""
    broker_server.responses["/v1/analysis-usage"] = (
        200,
        {"Content-Type": "application/json"},
        _broker_response(cost="0.31"),
    )
    status_path = analysis_monitor_root / "analysis-attempt-analysis.json"
    status_path.write_text(json.dumps({
        "attempt_id": "attempt-analysis",
        "plugin_version": "1.18.18",
        "policy_version": 1,
        "ready": True,
        "session_id": "session-analysis",
        "reminder_sent": True,
        "error": None,
    }), encoding="utf-8")
    monitor = AnalysisMonitor(
        attempt_id="attempt-analysis",
        policy={"version": 1, "termination_cost": "0.30"},
        database=analysis_monitor_root / "missing.db",
        directory=analysis_monitor_root,
        status_path=status_path,
        startup_deadline=0,
        broker_url=_broker_url(broker_server),
        model_capability="capability-" + "x" * 20,
    )

    with pytest.raises(AnalysisControlError) as failure:
        monitor.check("session-analysis")

    assert failure.value.code == "agent_maximum_analysis_depth_exceeded"
    assert failure.value.reason == "termination_cost_reached"
    assert monitor.observed_cost == Decimal("0.31")
    assert monitor.reminder_sent is True


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
