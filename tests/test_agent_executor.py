"""Agent executor 协议、认证、路径边界和取消语义测试。"""

from __future__ import annotations

import json
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from backend.agent_executor import AgentExecutorClient, AgentExecutorError
from backend.agent_executor import ExecutorTaskResponse
from backend.config import Settings
from backend.opencode import OpenCodeError, OpenCodeRunner
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
        "if os.getenv('FAKE_MODE') == 'provider429':\n"
        "    with open(directory + '/result.json.draft', 'w', encoding='utf-8') as handle: json.dump({'partial': True}, handle)\n"
        "    print(json.dumps({'type':'session.created','session_id':'session-' + task}), flush=True)\n"
        "    print(json.dumps({'type':'error','error':{'data':{'message':'provider status 429'}}}), flush=True)\n"
        "    sys.exit(7)\n"
        "if os.getenv('FAKE_MODE') == 'resume' and ('--session' not in sys.argv or 'session-' + task not in sys.argv):\n"
        "    print(json.dumps({'type':'error','error':{'data':{'message':'session missing'}}}), flush=True)\n"
        "    sys.exit(8)\n"
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
        bad.health()
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


def test_executor_rejects_malformed_json_with_stable_error(executor_fixture) -> None:
    """损坏的 JSON 请求不能把解析器原文变成不稳定错误码。"""
    _executor, client, _runtime = executor_fixture
    request = urllib.request.Request(
        f"{client.url}/v1/tasks",
        data=b"{not-json",
        headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {client.token}"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=2)
    payload = json.loads(error.value.read().decode("utf-8"))
    assert error.value.code == 400
    assert payload == {"error": "invalid_task", "message": "任务请求无效"}


def test_executor_client_waits_for_nonterminal_sync_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步提交即使先收到 queued/running，也必须轮询到终态后再交付结果。"""
    client = AgentExecutorClient("http://agent:8277", "executor-token", timeout=2)
    calls: list[tuple[str, str]] = []
    responses = iter(
        [
            (202, {"task_id": "queued-task", "status": "queued"}),
            (200, {"task_id": "queued-task", "status": "running"}),
            (200, {"task_id": "queued-task", "status": "succeeded", "session_id": "session-1"}),
        ]
    )

    def request(method: str, path: str, payload=None, *, timeout=None):
        calls.append((method, path))
        return next(responses)

    monkeypatch.setattr(client, "_request", request)
    result = client.run(task_id="queued-task", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=1)
    assert result.status == "succeeded"
    assert result.session_id == "session-1"
    assert calls == [("POST", "/v1/tasks"), ("GET", "/v1/tasks/queued-task"), ("GET", "/v1/tasks/queued-task")]


def test_executor_client_forwards_resume_source_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """续跑请求必须把上一条 executor attempt 作为明确绑定传给 executor。"""
    client = AgentExecutorClient("http://agent:8277", "executor-token", timeout=2)
    captured: dict[str, object] = {}

    def request(method: str, path: str, payload=None, *, timeout=None):
        """捕获受控请求中的恢复绑定字段。"""
        del method, path, timeout
        captured.update(payload or {})
        return 200, {
            "task_id": "resume-task",
            "business_task_id": "resume-task",
            "executor_attempt_id": "attempt-new",
            "status": "succeeded",
            "session_id": "session-1",
        }

    monkeypatch.setattr(client, "_request", request)
    response = client.run(
        task_id="resume-task",
        image_relative_path="sample.png",
        reverse_image_policy="forbid",
        timeout_seconds=5,
        executor_attempt_id="attempt-new",
        session_id="session-1",
        resume_of_attempt_id="attempt-old",
    )
    assert response.status == "succeeded"
    assert captured["resume_of_attempt_id"] == "attempt-old"


def test_executor_client_attempt_history_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """长期 API 进程只保留有限的业务 Task 到 executor attempt 映射。"""
    client = AgentExecutorClient("http://agent:8277", "executor-token", timeout=2)
    client._attempt_ids = {f"old-{index}": f"attempt-{index}" for index in range(5000)}

    def request(method: str, path: str, payload=None, *, timeout=None):
        """返回与本次请求 attempt 一致的最小成功响应。"""
        del method, path, timeout
        return 200, {
            "task_id": "new-task",
            "business_task_id": "new-task",
            "executor_attempt_id": (payload or {}).get("executor_attempt_id"),
            "status": "succeeded",
            "session_id": "session-new",
        }

    monkeypatch.setattr(client, "_request", request)
    client.run(task_id="new-task", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)
    assert len(client._attempt_ids) == 5000
    assert "old-0" not in client._attempt_ids


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


def test_executor_fences_concurrent_attempts_for_same_business_task(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一业务 Task 有活动 attempt 时，executor 拒绝并发启动第二个进程。"""
    executor, client, _runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_SLEEP": "5"})
    client._request(
        "POST",
        "/v1/tasks",
        {
            "task_id": "concurrent-task",
            "business_task_id": "concurrent-task",
            "executor_attempt_id": "attempt-first",
            "image_relative_path": "sample.png",
            "reverse_image_policy": "forbid",
            "timeout_seconds": 10,
            "wait": False,
        },
    )
    for _ in range(100):
        if executor.tasks["attempt-first"].status == "running":
            break
        time.sleep(0.01)
    with pytest.raises(AgentExecutorError) as error:
        client._request(
            "POST",
            "/v1/tasks",
            {
                "task_id": "concurrent-task",
                "business_task_id": "concurrent-task",
                "executor_attempt_id": "attempt-second",
                "image_relative_path": "sample.png",
                "reverse_image_policy": "forbid",
                "timeout_seconds": 10,
                "wait": False,
            },
        )
    assert error.value.code == "task_exists"
    client.cancel("attempt-first")


def test_executor_queue_backpressure_returns_stable_429(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行任务占用 worker 时，排队上限必须返回 429 而不是无限积压。"""
    executor, client, _runtime = executor_fixture
    executor.backpressure = 1
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_SLEEP": "2"})
    client._request(
        "POST",
        "/v1/tasks",
        {"task_id": "task-running", "image_relative_path": "sample.png", "timeout_seconds": 5, "wait": False},
    )
    for _ in range(100):
        if executor.tasks["task-running"].status == "running":
            break
        time.sleep(0.01)
    client._request(
        "POST",
        "/v1/tasks",
        {"task_id": "task-queued", "image_relative_path": "sample.png", "timeout_seconds": 5, "wait": False},
    )
    with pytest.raises(AgentExecutorError) as error:
        client._request(
            "POST",
            "/v1/tasks",
            {"task_id": "task-overflow", "image_relative_path": "sample.png", "timeout_seconds": 5, "wait": False},
        )
    assert error.value.code == "agent_backpressure"


def test_executor_captures_failed_session_and_resumes_with_new_attempt(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型网关失败仍保存 session，续跑使用新 attempt 并保留草稿。"""
    executor, client, runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_MODE": "provider429"})
    with pytest.raises(AgentExecutorError) as failure:
        client.run(task_id="resume-task", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)
    assert failure.value.code == "agent_provider_rate_limited"
    assert failure.value.session_id == "session-resume-task"
    assert failure.value.executor_attempt_id
    old_attempt = failure.value.executor_attempt_id
    assert (runtime / "task-results/resume-task/result.json.draft").exists()
    assert executor.tasks[old_attempt].status == "failed"

    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_MODE": "resume"})
    resumed = client.run(
        task_id="resume-task",
        image_relative_path="sample.png",
        reverse_image_policy="forbid",
        timeout_seconds=5,
        session_id=failure.value.session_id,
    )
    assert resumed.status == "succeeded"
    assert resumed.session_id == failure.value.session_id
    assert resumed.executor_attempt_id
    assert resumed.executor_attempt_id != old_attempt
    assert executor.tasks[old_attempt].status == "failed"


def test_executor_marks_unreaped_process_as_unknown_execution(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时终止未能确认父进程回收时，attempt 必须禁止自动续跑。"""
    executor, _client, _runtime = executor_fixture
    task = executor_server.TaskState(
        task_id="unreaped-task",
        business_task_id="unreaped-task",
        executor_attempt_id="attempt-unreaped",
        image_relative_path="sample.png",
        reverse_image_policy="forbid",
        timeout_seconds=1,
    )

    class UnreapedProcess:
        """模拟 kill 后仍无法由父进程 waitpid 回收的子进程。"""

        returncode = None
        pid = 1234

        def poll(self):
            """保持运行态，驱动 executor 的超时 fencing 分支。"""
            return None

    clock = iter((0.0, 2.0))
    monkeypatch.setattr(executor_server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(executor_server.subprocess, "Popen", lambda *_args, **_kwargs: UnreapedProcess())
    monkeypatch.setattr(executor, "_terminate", lambda _process: False)

    executor._run(task)

    assert task.status == "failed"
    assert task.process_reaped is False
    assert task.error == {"error": "unknown_execution", "message": "无法确认 OpenCode 进程已终止"}


def test_executor_rejects_unreaped_attempt_as_resume_source(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """内存中的未回收失败 attempt 不得被 session 续跑重新采用。"""
    executor, client, _runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_MODE": "provider429"})
    with pytest.raises(AgentExecutorError) as failure:
        client.run(task_id="unreaped-source", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)
    source = executor.tasks[failure.value.executor_attempt_id]
    source.process_reaped = False

    with pytest.raises(AgentExecutorError) as resume_failure:
        client.run(
            task_id="unreaped-source",
            image_relative_path="sample.png",
            reverse_image_policy="forbid",
            timeout_seconds=5,
            session_id=failure.value.session_id,
        )
    assert resume_failure.value.code == "unknown_execution"


def test_executor_rejects_session_binding_mismatch_and_reused_attempt(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """续跑必须绑定同一业务事实，终态 attempt ID 不得重复提交。"""
    executor, client, _runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_MODE": "provider429"})
    with pytest.raises(AgentExecutorError) as failure:
        client.run(task_id="binding-task", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)
    attempt_id = failure.value.executor_attempt_id
    assert attempt_id
    with pytest.raises(AgentExecutorError) as mismatch:
        client.run(
            task_id="binding-task",
            image_relative_path="sample.png",
            reverse_image_policy="forbid",
            timeout_seconds=5,
            session_id=failure.value.session_id,
            processing_config_hash="a" * 64,
        )
    assert mismatch.value.code == "session_binding_mismatch"
    with pytest.raises(AgentExecutorError) as duplicate:
        client._request(
            "POST",
            "/v1/tasks",
            {
                "task_id": "binding-task",
                "business_task_id": "binding-task",
                "executor_attempt_id": attempt_id,
                "image_relative_path": "sample.png",
                "reverse_image_policy": "forbid",
                "timeout_seconds": 5,
                "wait": False,
            },
        )
    assert duplicate.value.code == "task_exists"


def test_executor_restart_uses_signed_attempt_metadata(executor_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """executor 重启后只从签名 attempt 元数据恢复同一失败 session。"""
    executor, client, _runtime = executor_fixture
    original_environment = executor._task_environment
    monkeypatch.setattr(executor, "_task_environment", lambda task: {**original_environment(task), "FAKE_MODE": "provider429"})
    with pytest.raises(AgentExecutorError) as failure:
        client.run(task_id="restart-task", image_relative_path="sample.png", reverse_image_policy="forbid", timeout_seconds=5)

    executor.close()
    restarted = executor_server.Executor()
    monkeypatch.setattr(restarted, "health", lambda: {"ready": True})
    restarted_environment = restarted._task_environment
    monkeypatch.setattr(restarted, "_task_environment", lambda task: {**restarted_environment(task), "FAKE_MODE": "resume"})
    try:
        task, wait = restarted.submit(
            {
                "task_id": "restart-task",
                "business_task_id": "restart-task",
                "executor_attempt_id": "attempt-after-restart",
                "session_id": failure.value.session_id,
                "image_relative_path": "sample.png",
                "reverse_image_policy": "forbid",
                "timeout_seconds": 5,
                "wait": True,
            }
        )
        assert wait is True
        assert task.is_resume is True
        assert task.done.wait(2)
        assert task.status == "succeeded"
        assert task.resume_of_attempt_id == failure.value.executor_attempt_id
    finally:
        restarted.close()


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


def test_runner_executor_mode_rejects_success_without_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """executor 成功响应缺少 session 时不能把业务 task ID 冒充恢复标识。"""
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
    monkeypatch.setattr(runner.executor, "run", lambda **kwargs: ExecutorTaskResponse(kwargs["task_id"], "succeeded", None, None, None, "attempt-no-session"))

    with pytest.raises(OpenCodeError) as failure:
        runner.run(image, lambda *_args: None, task_id="task-without-session")

    assert failure.value.code == "agent_output_invalid_json"
    assert failure.value.session_id is None
