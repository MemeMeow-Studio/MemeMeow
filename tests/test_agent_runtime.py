"""共享 Agent 容器边界和任务结果文件协议测试。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import uuid
import asyncio
import stat
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from starlette.requests import Request

from api import config_status
from backend.config import Settings
from backend.opencode import OpenCodeError, OpenCodeRunner


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """构造仅使用临时目录的 Docker runtime 配置。"""
    values: dict[str, object] = {
        "data_root": tmp_path / "data",
        "image_root": tmp_path / "data" / "images",
        "opencode_model": "mememeow/gpt-5.6-luna",
        "opencode_base_url": "https://example.invalid/v1",
        "opencode_api_key": "test-key",
        "agent_container_name": "mememeow-agent-runtime",
        "agent_runtime_mode": "docker",
    }
    values.update(overrides)
    return Settings(**values)


def candidate() -> dict[str, object]:
    """返回满足输出 schema 的最小业务对象。"""
    return {
        "title": "测试标题",
        "summary": "测试摘要",
        "subjects": ["主体"],
        "visible_text": [],
        "references": [],
        "meaning": None,
        "keywords": ["测试"],
        "search_queries": [],
        "uncertainties": [],
        "source_urls": [],
    }


def test_container_command_whitelists_environment_and_maps_image(tmp_path: Path):
    """容器命令使用 /images 路径且不继承宿主密钥或数据库环境。"""
    image_root = tmp_path / "data" / "images"
    image_root.mkdir(parents=True)
    image = image_root / "nested" / "meme.png"
    image.parent.mkdir()
    image.write_bytes(b"image")
    runner = OpenCodeRunner(make_settings(tmp_path), project_root=tmp_path)
    command = runner.container_command(image, "write result", task_id="task-1")
    assert command[:3] == ["docker", "exec", "--workdir"]
    assert "/images/nested/meme.png" in command
    assert "--auto" in command
    assert "--env" in command
    assert "test-key" not in " ".join(command)
    assert all("=" not in value for index, value in enumerate(command) if index and command[index - 1] == "--env")
    assert not any("DATABASE_URL" in value or "HOME=" in value for value in command)
    assert "SERPAPI_API_KEY" not in command


def test_docker_environment_contains_claim_task_id(tmp_path: Path):
    """Docker 白名单环境必须和 Host 一样传递当前 claim task id。"""
    runner = OpenCodeRunner(make_settings(tmp_path), project_root=tmp_path)
    environment = runner._allowed_container_environment(1, "claim-task-123")
    assert environment["MEMEMEOW_AGENT_TASK_ID"] == "claim-task-123"
    assert "SERPAPI_API_KEY" not in environment


def test_docker_environment_uses_agent_callback_urls(tmp_path: Path):
    """宿主后端与 Docker Agent 分离时，容器应使用专用 host-gateway 回调地址。"""
    runner = OpenCodeRunner(
        make_settings(
            tmp_path,
            agent_reverse_image_internal_url="http://host.docker.internal:8275/internal/reverse-image/search",
            agent_visual_search_internal_url="http://host.docker.internal:8275/internal/visual-search/match",
        ),
        project_root=tmp_path,
    )
    environment = runner._allowed_container_environment(0, "claim-task-urls")
    assert environment["MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL"].startswith("http://host.docker.internal:")
    assert environment["MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL"].startswith("http://host.docker.internal:")


def test_image_path_outside_root_is_rejected(tmp_path: Path):
    """图片根目录外的路径不能映射到 Agent。"""
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    runner = OpenCodeRunner(make_settings(tmp_path), project_root=tmp_path)
    with pytest.raises(OpenCodeError) as error:
        runner.map_host_image_path(outside)
    assert error.value.code == "agent_image_path_forbidden"


def test_image_symlink_is_rejected_before_resolution(tmp_path: Path):
    """图片根目录内的符号链接也不能借路径解析绕过只读边界。"""
    image_root = tmp_path / "data" / "images"
    image_root.mkdir(parents=True)
    source = image_root / "source.png"
    source.write_bytes(b"image")
    link = image_root / "link.png"
    link.symlink_to(source)
    runner = OpenCodeRunner(make_settings(tmp_path), project_root=tmp_path)
    with pytest.raises(OpenCodeError) as error:
        runner.map_host_image_path(link)
    assert error.value.code == "agent_image_path_forbidden"


def test_docker_image_root_mismatch_is_rejected_before_container_exec(tmp_path: Path):
    """Docker 固定 `/images` 挂载与自定义图片根不一致时稳定阻断任务。"""
    custom_root = tmp_path / "custom-images"
    custom_root.mkdir()
    runner = OpenCodeRunner(make_settings(tmp_path, image_root=custom_root))
    with pytest.raises(OpenCodeError) as error:
        runner.prepare_runtime()
    assert error.value.code == "agent_image_root_mismatch"


def test_runtime_probe_strictly_checks_non_root_and_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """运行时探针必须把 UID 0 和可写依赖目录判为不安全。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    monkeypatch.setattr(runner, "_container_ready", lambda: (True, "ok"))
    monkeypatch.setattr(runner, "_container_exec_probe", lambda arguments: (False, "uid=0") if arguments[:2] == ["sh", "-lc"] and "id -u" in arguments[-1] else (True, "ok"))
    result = runner.runtime_probe()
    assert result["non_root"] is False
    assert result["verified"] is False


def test_config_exposes_sanitized_agent_runtime_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`/config` 只返回固定 runtime 标识和布尔状态，不泄露诊断或密钥。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    monkeypatch.setattr(
        runner,
        "runtime_probe",
        lambda: {
            "mode": "shared-docker-container",
            "container_name": "mememeow-agent-runtime",
            "container_running": True,
            "runtime_root_ready": True,
            "workspace_ready": True,
            "executable_ready": True,
            "skills_ready": True,
            "dependencies_ready": True,
            "mounts_ready": True,
            "non_root": True,
            "network_ready": True,
            "docker_socket_absent": True,
            "verified": True,
            "container_diagnostic": "宿主绝对路径不应返回",
        },
    )
    app_stub = SimpleNamespace(
        state=SimpleNamespace(
            settings=runner.settings,
            opencode=runner,
            search_engine=SimpleNamespace(has_cache=lambda: False),
        )
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/config",
        "raw_path": b"/config",
        "query_string": b"",
        "headers": [],
        "app": app_stub,
    })
    payload = asyncio.run(config_status(request))
    assert payload["runtime_ready"] is True
    assert payload["agent_runtime"]["mode"] == "shared-docker-container"
    assert "container_diagnostic" not in payload["agent_runtime"]
    assert "opencode_api_key" not in payload


def test_result_path_failure_releases_slot(tmp_path: Path):
    """任务结果路径冲突时不能泄漏已获取的 slot。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    runner.prepare_runtime = lambda: None
    task_directory = runner.runtime_root / "task-results" / "slot-conflict"
    task_directory.mkdir(parents=True)
    (task_directory / "result.json.tmp").mkdir()
    with pytest.raises(OpenCodeError) as error:
        runner.run(tmp_path / "image.png", lambda *_: None, task_id="slot-conflict")
    assert error.value.code == "agent_result_path_invalid"
    assert runner._slot_semaphore.acquire(timeout=0.1)
    runner._slot_semaphore.release()


def _run_container(runner: OpenCodeRunner, *arguments: str) -> subprocess.CompletedProcess[str]:
    """通过与生产运行器相同的 Docker/sg 包装执行容器命令。"""
    command = runner._docker_command_for_execution(runner._container_exec(*arguments))
    return subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)


@pytest.mark.skipif(os.getenv("MEMEMEOW_AGENT_RUNTIME_E2E") != "1", reason="显式设置 MEMEMEOW_AGENT_RUNTIME_E2E=1 才运行 Docker 集成验收")
def test_shared_container_runs_distinct_sessions_and_result_directories():
    """两个真实 docker exec 任务共享容器，但使用不同 session 和结果目录。"""
    project_root = Path(__file__).resolve().parent.parent
    settings = Settings.from_env(project_root / ".env")
    settings.agent_runtime_mode = "docker"
    settings.agent_container_name = os.getenv("MEMEMEOW_AGENT_CONTAINER_NAME", "mememeow-agent-runtime")
    runner = OpenCodeRunner(settings, project_root=project_root)
    if not runner.runtime_probe().get("verified"):
        pytest.skip("共享 Agent 容器未运行或探针未通过")
    runner.prepare_runtime()
    image = settings.image_root / "example_meme_1.jpg"
    if not image.is_file():
        pytest.skip("缺少 data/images/example_meme_1.jpg 集成图片")

    suffix = uuid.uuid4().hex
    task_ids = (f"integration-{suffix}-a", f"integration-{suffix}-b")
    stub_path = f"/runtime/workspace/.mememeow-agent-stub-{suffix}"
    stub = """#!/bin/sh
set -eu
task="${MEMEMEOW_AGENT_TASK_ID:?}"
directory="/runtime/task-results/$task"
mkdir -p "$directory"
printf '%s\\n' '{"title":"集成验收","summary":"共享容器结果","subjects":["主体"],"visible_text":[],"references":[],"meaning":null,"keywords":["集成"],"search_queries":[],"uncertainties":[],"source_urls":[]}' > "$directory/result.json.draft"
mv "$directory/result.json.draft" "$directory/result.json.tmp"
printf '{"type":"session.created","session_id":"stub-%s"}\\n' "$task"
"""
    escaped_stub = shlex.quote(stub)
    setup = _run_container(runner, "sh", "-lc", f"printf %s {escaped_stub} > {shlex.quote(stub_path)} && chmod 755 {shlex.quote(stub_path)}")
    assert setup.returncode == 0, setup.stderr
    original_executable = settings.opencode_executable
    settings.opencode_executable = stub_path
    try:
        first, first_session = runner.run(image, lambda *_: None, task_id=task_ids[0])
        second, second_session = runner.run(image, lambda *_: None, task_id=task_ids[1])
        assert first["title"] == second["title"] == "集成验收"
        assert first_session != second_session
        assert (runner.runtime_root / "task-results" / task_ids[0] / "result.json.tmp").is_file()
        assert (runner.runtime_root / "task-results" / task_ids[1] / "result.json.tmp").is_file()
        boundary = _run_container(
            runner,
            "sh",
            "-lc",
            "test \"$(id -u)\" != 0 && test -r /images/example_meme_1.jpg && test ! -w /images && "
            "test -r /skills/research-meme-context && test ! -w /skills/research-meme-context && "
            "test -r /opt/mememeow/node_modules && test ! -w /opt/mememeow/node_modules && "
            "test -r /runtime && test -w /runtime && test ! -e /.env && test ! -S /var/run/docker.sock",
        )
        assert boundary.returncode == 0, boundary.stderr
    finally:
        settings.opencode_executable = original_executable
        _run_container(runner, "sh", "-lc", f"rm -f {shlex.quote(stub_path)}")
        for task_id in task_ids:
            shutil.rmtree(runner.runtime_root / "task-results" / task_id, ignore_errors=True)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("missing", "agent_result_file_missing"), ("invalid", "agent_result_file_invalid_json"), ("schema", "agent_result_file_schema_invalid"), ("large", "agent_result_file_too_large")],
)
def test_result_file_failures_have_stable_codes(tmp_path: Path, kind: str, expected: str):
    """结果文件缺失、JSON 截断、schema 错误和超限均不回退 assistant 文本。"""
    runner = OpenCodeRunner(make_settings(tmp_path, agent_result_max_bytes=1024))
    _draft, result_path = runner.create_task_result_paths("task-1")
    if kind == "invalid":
        result_path.write_text('{"title":', encoding="utf-8")
    elif kind == "schema":
        result_path.write_text(json.dumps({"title": "only-title"}), encoding="utf-8")
    elif kind == "large":
        result_path.write_bytes(b"x" * 1025)
    with pytest.raises(OpenCodeError) as error:
        runner.read_result_file(result_path)
    assert error.value.code == expected


def test_result_file_success_is_schema_validated_and_task_directories_are_independent(tmp_path: Path):
    """两个任务使用不同结果目录，成功结果只来自最终临时文件。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    first_draft, first_result = runner.create_task_result_paths("task-a")
    second_draft, second_result = runner.create_task_result_paths("task-b")
    assert first_result.parent != second_result.parent
    first_draft.write_text(json.dumps(candidate(), ensure_ascii=False), encoding="utf-8")
    first_draft.replace(first_result)
    second_result.write_text(json.dumps(candidate(), ensure_ascii=False), encoding="utf-8")
    assert runner.read_result_file(first_result)["title"] == "测试标题"
    assert runner.read_result_file(second_result)["title"] == "测试标题"


def test_retry_clears_previous_result_artifact(tmp_path: Path):
    """同一任务重试前必须移除旧最终文件，防止失败尝试误读历史结果。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    draft, result = runner.create_task_result_paths("retry-task")
    result.write_text(json.dumps(candidate(), ensure_ascii=False), encoding="utf-8")
    runner._reset_task_result_files(draft, result)
    assert not draft.exists()
    assert not result.exists()


@pytest.mark.parametrize("kind", ["result", "draft", "directory"])
def test_result_paths_and_reads_reject_symlink_hijacking(tmp_path: Path, kind: str):
    """结果最终文件、草稿和任务目录均不能通过符号链接逃逸。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    root = runner.runtime_root / "task-results"
    root.mkdir(parents=True)
    if kind == "directory":
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "task-link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(OpenCodeError) as error:
            runner.create_task_result_paths("task-link")
    else:
        draft, result = runner.create_task_result_paths("task-link")
        target = draft if kind == "draft" else result
        target.unlink(missing_ok=True)
        outside = tmp_path / f"outside-{kind}.json"
        outside.write_text(json.dumps(candidate()), encoding="utf-8")
        target.symlink_to(outside)
        with pytest.raises(OpenCodeError) as error:
            if kind == "draft":
                runner._reset_task_result_files(draft, result)
            else:
                runner.read_result_file(result)
    assert error.value.code in {"agent_result_path_invalid", "agent_result_file_unreadable"}


def test_result_parent_symlink_is_rejected(tmp_path: Path):
    """task-results 本身被替换为符号链接时不能创建任务目录。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    runner.runtime_root.mkdir(parents=True)
    outside = tmp_path / "outside-results"
    outside.mkdir()
    (runner.runtime_root / "task-results").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OpenCodeError) as error:
        runner.create_task_result_paths("task-1")
    assert error.value.code == "agent_result_path_invalid"


def test_cleanup_preserves_active_task_directories(tmp_path: Path):
    """清理旧产物时必须跳过其他仍在运行 task 的结果目录。"""
    runner = OpenCodeRunner(make_settings(tmp_path, agent_result_retention_days=1, agent_result_max_tasks=1))
    active = runner.runtime_root / "task-results" / "active-task"
    old = runner.runtime_root / "task-results" / "old-task"
    active.mkdir(parents=True)
    old.mkdir(parents=True)
    old_mtime = time.time() - 3 * 86400
    os.utime(active, (old_mtime, old_mtime))
    os.utime(old, (old_mtime, old_mtime))
    runner._mark_task_active("active-task")
    try:
        assert runner.cleanup_task_results() == 1
        assert active.is_dir()
        assert not old.exists()
    finally:
        runner._unmark_task_active("active-task")


def test_terminate_container_session_waits_and_escalates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """容器任务终止应发送 TERM、等待确认，再发送 KILL 兜底。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    calls: list[str] = []
    alive = {"value": True}
    ignore_term = {"value": False}
    def signal_processes(pattern: str, signal_name: str) -> None:
        calls.append(signal_name)
        if signal_name == "TERM" and not ignore_term["value"]:
            alive["value"] = False
    monkeypatch.setattr(runner, "_container_signal_processes", signal_processes)
    monkeypatch.setattr(runner, "_container_has_task_processes", lambda pattern: alive["value"])
    runner._terminate_container_session("task-1")
    assert calls == ["TERM"]

    calls.clear()
    alive["value"] = True
    ignore_term["value"] = True
    runner._terminate_container_session("task-2")
    assert calls[:2] == ["TERM", "KILL"]
