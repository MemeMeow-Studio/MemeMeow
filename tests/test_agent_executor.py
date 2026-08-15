"""Agent executor 协议、认证、路径边界和取消语义测试。"""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import pytest

from backend.agent_executor import AgentExecutorClient, AgentExecutorError
from backend.agent_executor import ExecutorTaskResponse
from backend.config import Settings
from backend.opencode import OpenCodeRunner
from executor import server as executor_server
from executor.token import ExecutorTokenError, ensure_token_file, read_token_file


def _candidate() -> dict[str, object]:
    """返回满足后端结果文件基本契约的 stub 结果。"""
    return {
        "title": "executor 测试",
        "summary": "受控任务结果",
        "subjects": ["主体"],
        "visible_text": [],
        "references": [],
        "meaning": None,
        "keywords": ["测试"],
        "search_queries": [],
        "uncertainties": [],
        "source_urls": [],
    }


def test_executor_token_file_is_random_persistent_and_permission_limited(tmp_path: Path) -> None:
    """首次启动生成 0600 token，后续读取必须复用同一凭据。"""
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir(mode=0o700)
    token_path = secret_dir / "token"

    token = ensure_token_file(token_path)
    assert len(token) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert read_token_file(token_path) == token
    assert ensure_token_file(token_path) == token


def test_executor_token_file_rejects_insecure_permissions_and_symlinks(tmp_path: Path) -> None:
    """token 文件的 group/other 权限和符号链接必须阻断启动。"""
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir(mode=0o700)
    token_path = secret_dir / "token"
    token_path.write_text("x" * 43 + "\n", encoding="utf-8")
    token_path.chmod(0o640)
    with pytest.raises(ExecutorTokenError, match="permissions_invalid"):
        read_token_file(token_path)

    link = secret_dir / "link"
    link.symlink_to(token_path)
    with pytest.raises(ExecutorTokenError):
        read_token_file(link)


def test_settings_prefers_compose_token_file_over_dotenv_token(tmp_path: Path) -> None:
    """Compose API 必须读取共享文件，避免旧 dotenv token 与 executor 分叉。"""
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir(mode=0o700)
    token_path = secret_dir / "token"
    token = ensure_token_file(token_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE={token_path}\n"
        "MEMEMEOW_AGENT_EXECUTOR_TOKEN=stale-dotenv-token\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(env_file)
    assert settings.agent_executor_token == token


@pytest.fixture
def executor_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """启动带 fake OpenCode 的本地 executor HTTP 服务。"""
    runtime = tmp_path / "runtime"
    images = tmp_path / "images"
    skill = tmp_path / "skill"
    runtime.mkdir()
    images.mkdir()
    skill.mkdir()
    (images / "sample.png").write_bytes(b"image")
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "if os.getenv('FAKE_SLEEP'):\n"
        "    time.sleep(float(os.environ['FAKE_SLEEP']))\n"
        "task = os.environ['MEMEMEOW_AGENT_TASK_ID']\n"
        "directory = os.path.dirname(os.environ['OPENCODE_DB']) + '/task-results/' + task\n"
        "os.makedirs(directory, exist_ok=True)\n"
        "candidate = {'title':'executor 测试','summary':'受控任务结果','subjects':['主体'],'visible_text':[],'references':[],'meaning':None,'keywords':['测试'],'search_queries':[],'uncertainties':[],'source_urls':[]}\n"
        "with open(directory + '/result.json.draft', 'w', encoding='utf-8') as handle: json.dump(candidate, handle, ensure_ascii=False)\n"
        "os.replace(directory + '/result.json.draft', directory + '/result.json.tmp')\n"
        "print(json.dumps({'type':'session.created','session_id':'session-' + task}), flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    skill.chmod(0o555)
    images.chmod(0o555)
    (images / "sample.png").chmod(0o444)
    monkeypatch.setattr(executor_server, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(executor_server, "WORKSPACE", runtime / "workspace")
    monkeypatch.setattr(executor_server, "RESULT_ROOT", runtime / "task-results")
    monkeypatch.setattr(executor_server, "LOG_ROOT", runtime / "logs")
    monkeypatch.setattr(executor_server, "IMAGE_ROOT", images)
    monkeypatch.setattr(executor_server, "SKILL_ROOT", skill)
    monkeypatch.setenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "executor-test-token")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "mememeow/gpt-5.6-luna")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_API_KEY", "test-key")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_EXECUTABLE", str(fake))
    executor = executor_server.Executor()
    monkeypatch.setattr(executor, "health", lambda: {"ready": True, "docker_socket_absent": True})
    http = executor_server.ExecutorHTTPServer(("127.0.0.1", 0), executor)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    client = AgentExecutorClient(f"http://127.0.0.1:{http.server_port}", "executor-test-token", timeout=10)
    try:
        yield executor, client, runtime
    finally:
        executor.close()
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_executor_rejects_bad_token_and_arbitrary_fields(executor_fixture) -> None:
    """认证失败和任意命令字段必须在执行前被拒绝。"""
    _executor, client, _runtime = executor_fixture
    bad = AgentExecutorClient(client.url, "wrong-token", timeout=2)
    with pytest.raises(AgentExecutorError) as error:
        bad.status("task-1")
    assert error.value.code == "agent_executor_unauthorized"
    with pytest.raises(AgentExecutorError) as error:
        client._request("POST", "/v1/tasks", {"task_id": "task-1", "image_relative_path": "sample.png", "command": "id"})
    assert error.value.code == "invalid_task"


def test_executor_runs_fixed_task_and_returns_result(executor_fixture) -> None:
    """固定任务只能读取 images 相对路径，并通过共享结果文件传递结果。"""
    _executor, client, runtime = executor_fixture
    result = client.run(task_id="task-success", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)
    assert result.status == "succeeded"
    assert result.session_id == "session-task-success"
    payload = json.loads((runtime / "task-results/task-success/result.json.tmp").read_text(encoding="utf-8"))
    assert payload["title"] == "executor 测试"
    with pytest.raises(AgentExecutorError) as error:
        client._request("POST", "/v1/tasks", {"task_id": "task-path", "image_relative_path": "../outside.png", "wait": True})
    assert error.value.code == "agent_image_path_forbidden"


def test_executor_cancel_terminates_only_one_task(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消 queued/running 任务必须返回稳定状态而不停止 executor 服务。"""
    executor, client, _runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_SLEEP": "5"})
    _status, queued = client._request(
        "POST",
        "/v1/tasks",
        {"task_id": "task-cancel", "image_relative_path": "sample.png", "timeout_seconds": 10, "wait": False},
    )
    assert queued["status"] in {"queued", "running"}
    for _ in range(100):
        if executor.tasks["task-cancel"].status == "running":
            break
        time.sleep(0.01)
    cancelled = client.cancel("task-cancel")
    assert cancelled.status == "cancelled"
    assert client.health()["ready"] is True


def test_runner_executor_mode_uses_http_without_docker_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose runner 只构造 HTTP 任务，不探测或启动 Docker CLI。"""
    data = tmp_path / "data"
    image_root = data / "images"
    image_root.mkdir(parents=True)
    image = image_root / "sample.png"
    image.write_bytes(b"image")
    settings = Settings(
        data_root=data,
        image_root=image_root,
        opencode_model="mememeow/gpt-5.6-luna",
        opencode_base_url="https://example.invalid/v1",
        opencode_api_key="test-key",
        agent_runtime_mode="executor",
        agent_executor_url="http://agent:8277",
        agent_executor_token="executor-token",
    )
    runner = OpenCodeRunner(settings, project_root=tmp_path)
    monkeypatch.setattr(runner.executor, "health", lambda: {"ready": True})
    monkeypatch.setattr(runner, "validate_candidate", lambda value: value)

    def run_http(**kwargs):
        """模拟 executor 写入共享结果文件并返回 session。"""
        result_dir = runner.runtime_root / "task-results" / kwargs["task_id"]
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json.tmp").write_text(json.dumps(_candidate(), ensure_ascii=False), encoding="utf-8")
        return ExecutorTaskResponse(kwargs["task_id"], "succeeded", "session-http", None, "task-results/result.json.tmp")

    monkeypatch.setattr(runner.executor, "run", run_http)
    monkeypatch.setattr(executor_server.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Compose runner must not start local subprocess")))
    result, session = runner.run(image, lambda *_args: None, task_id="http-task")
    assert result["title"] == "executor 测试"
    assert session == "session-http"
