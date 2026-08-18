"""Agent 运行模式、executor 边界和任务结果文件协议测试。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from api import config_status
from backend.agent_executor import AgentExecutorError
from backend.config import Settings
from backend.opencode import OpenCodeError, OpenCodeRunner


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """构造仅使用临时目录的 host/executor 测试配置。"""
    values: dict[str, object] = {
        "_env_file": None,
        "data_root": tmp_path / "data",
        "image_root": tmp_path / "data" / "images",
        "opencode_model": "mememeow/gpt-5.6-luna",
        "opencode_base_url": "https://example.invalid/v1",
        "opencode_api_key": "test-key",
        "opencode_executable": None,
        "agent_runtime_mode": "host",
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


def test_runtime_mode_selection_is_executor_or_host(tmp_path: Path) -> None:
    """auto 只在 URL/token 均存在时选择 executor，显式 host 始终保留本地回滚。"""
    assert OpenCodeRunner(make_settings(tmp_path)).executor_mode is False
    assert OpenCodeRunner(make_settings(tmp_path, agent_runtime_mode="auto")).executor_mode is False
    assert OpenCodeRunner(make_settings(tmp_path, agent_runtime_mode="auto", agent_executor_url="http://agent:8277")).executor_mode is False
    assert OpenCodeRunner(make_settings(tmp_path, agent_runtime_mode="auto", agent_executor_url=" ", agent_executor_token=" ")).executor_mode is False
    assert OpenCodeRunner(
        make_settings(tmp_path, agent_runtime_mode="auto", agent_executor_url="http://agent:8277", agent_executor_token="token")
    ).executor_mode is True
    assert OpenCodeRunner(
        make_settings(tmp_path, agent_runtime_mode="host", agent_executor_url="http://agent:8277", agent_executor_token="token")
    ).executor_mode is False


def test_explicit_executor_missing_configuration_fails_closed(tmp_path: Path) -> None:
    """显式 executor 缺少 URL 或 token 时失败，不能静默启动 host OpenCode。"""
    runner = OpenCodeRunner(make_settings(tmp_path, agent_runtime_mode="executor"))
    assert runner.executor_mode is True
    with pytest.raises(OpenCodeError) as error:
        runner.prepare_runtime()
    assert error.value.code == "agent_executor_not_configured"


def test_legacy_docker_mode_and_fields_are_rejected(tmp_path: Path) -> None:
    """旧 docker mode 被拒绝，旧容器字段不会成为 Settings 属性或执行开关。"""
    with pytest.raises(ValidationError, match="agent_runtime_mode_invalid"):
        make_settings(tmp_path, agent_runtime_mode="docker")
    settings = make_settings(tmp_path, MEMEMEOW_AGENT_CONTAINER_NAME="legacy-name", MEMEMEOW_AGENT_CONTAINER_RUNTIME="docker")
    assert not hasattr(settings, "agent_container_name")
    assert not hasattr(settings, "agent_container_runtime")


@pytest.mark.parametrize("executor_error", ["agent_executor_unavailable", "agent_executor_unauthorized"])
def test_executor_failure_does_not_fallback_to_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executor_error: str) -> None:
    """executor 请求失败必须保留稳定错误码，不调用宿主 subprocess 回退。"""
    data_root = tmp_path / "data"
    image_root = data_root / "images"
    image_root.mkdir(parents=True)
    image = image_root / "sample.png"
    image.write_bytes(b"image")
    runner = OpenCodeRunner(
        make_settings(
            tmp_path,
            agent_runtime_mode="executor",
            agent_executor_url="http://agent:8277",
            agent_executor_token="token",
        ),
        project_root=tmp_path,
    )
    monkeypatch.setattr(runner.executor, "health", lambda: {"ready": True})
    monkeypatch.setattr(runner.executor, "run", lambda **_kwargs: (_ for _ in ()).throw(AgentExecutorError(executor_error)))
    monkeypatch.setattr(runner, "_run_command", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("host fallback")))
    with pytest.raises(OpenCodeError) as error:
        runner.run(image, lambda *_args: None, task_id="executor-failure")
    assert error.value.code == executor_error


def test_executor_health_failure_is_stable_and_does_not_fallback_to_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """executor 健康探针异常必须转换为稳定错误，不能启动宿主 OpenCode。"""
    runner = OpenCodeRunner(
        make_settings(tmp_path, agent_runtime_mode="executor", agent_executor_url="http://agent:8277", agent_executor_token="token"),
        project_root=tmp_path,
    )
    monkeypatch.setattr(runner.executor, "health", lambda: (_ for _ in ()).throw(AgentExecutorError("agent_executor_unauthorized")))
    monkeypatch.setattr(runner, "_run_command", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("host fallback")))
    image = tmp_path / "data" / "images" / "sample.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    with pytest.raises(OpenCodeError) as error:
        runner.run(image, lambda *_args: None, task_id="executor-health-failure")
    assert error.value.code == "agent_executor_unauthorized"


def test_executor_image_path_is_mapped_and_host_path_is_checked(tmp_path: Path) -> None:
    """executor 图片只能映射到 /images，根目录外和符号链接均被拒绝。"""
    image_root = tmp_path / "data" / "images"
    image_root.mkdir(parents=True)
    image = image_root / "nested" / "meme.png"
    image.parent.mkdir()
    image.write_bytes(b"image")
    runner = OpenCodeRunner(
        make_settings(tmp_path, agent_runtime_mode="executor", agent_executor_url="http://agent:8277", agent_executor_token="token"),
        project_root=tmp_path,
    )
    assert runner.map_image_path(image).as_posix() == "/images/nested/meme.png"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(OpenCodeError) as error:
        runner.map_image_path(outside)
    assert error.value.code == "agent_image_path_forbidden"
    link = image_root / "link.png"
    link.symlink_to(image)
    with pytest.raises(OpenCodeError) as error:
        runner.map_image_path(link)
    assert error.value.code == "agent_image_path_forbidden"


def test_executor_runtime_probe_has_no_real_container_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """executor 探针只返回健康布尔值，不暴露 Compose 生成的真实实例名。"""
    runner = OpenCodeRunner(
        make_settings(tmp_path, agent_runtime_mode="executor", agent_executor_url="http://agent:8277", agent_executor_token="token"),
    )
    monkeypatch.setattr(
        runner.executor,
        "health",
        lambda: {
            "ready": True,
            "runtime_read_write": True,
            "images_read_only": True,
            "skills_read_only": True,
            "opencode": True,
            "docker_socket_absent": True,
        },
    )
    result = runner.runtime_probe()
    assert result["mode"] == "executor"
    assert result["executor_running"] is True
    assert result["verified"] is True
    assert "container_name" not in result


def test_config_exposes_sanitized_agent_runtime_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/config` 只返回固定 runtime 标识和布尔状态，不泄露诊断、名称或密钥。"""
    runner = OpenCodeRunner(make_settings(tmp_path, agent_runtime_mode="host"))
    monkeypatch.setattr(
        runner,
        "runtime_probe",
        lambda: {
            "mode": "host-runtime-slot-lock",
            "executor_running": False,
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
            "container_name": "must-not-leak",
            "container_diagnostic": "must-not-leak",
        },
    )
    app_stub = SimpleNamespace(state=SimpleNamespace(settings=runner.settings, opencode=runner, search_engine=SimpleNamespace(has_cache=lambda: False)))
    request = Request({"type": "http", "method": "GET", "path": "/config", "raw_path": b"/config", "query_string": b"", "headers": [], "app": app_stub})
    payload = asyncio.run(config_status(request))
    assert payload["runtime_ready"] is True
    assert payload["agent_runtime"]["mode"] == "host-runtime-slot-lock"
    assert "container_name" not in payload["agent_runtime"]
    assert "container_diagnostic" not in payload["agent_runtime"]
    assert "opencode_api_key" not in payload


def test_result_path_failure_releases_slot(tmp_path: Path) -> None:
    """任务结果路径冲突时不能泄漏已获取的 slot。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    runner.prepare_runtime = lambda: None
    task_directory = runner.runtime_root / "task-results" / "slot-conflict"
    task_directory.mkdir(parents=True)
    (task_directory / "result.json.tmp").mkdir()
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    with pytest.raises(OpenCodeError) as error:
        runner.run(image, lambda *_args: None, task_id="slot-conflict")
    assert error.value.code == "agent_result_path_invalid"
    assert runner._slot_semaphore.acquire(timeout=0.1)
    runner._slot_semaphore.release()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("missing", "agent_result_file_missing"), ("invalid", "agent_result_file_invalid_json"), ("schema", "agent_result_file_schema_invalid"), ("large", "agent_result_file_too_large")],
)
def test_result_file_failures_have_stable_codes(tmp_path: Path, kind: str, expected: str) -> None:
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


def test_result_file_success_is_schema_validated_and_task_directories_are_independent(tmp_path: Path) -> None:
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


def test_retry_clears_previous_result_artifact(tmp_path: Path) -> None:
    """同一任务重试前必须移除旧最终文件，防止失败尝试误读历史结果。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    draft, result = runner.create_task_result_paths("retry-task")
    result.write_text(json.dumps(candidate(), ensure_ascii=False), encoding="utf-8")
    runner._reset_task_result_files(draft, result)
    assert not draft.exists()
    assert not result.exists()


@pytest.mark.parametrize("kind", ["result", "draft", "directory"])
def test_result_paths_and_reads_reject_symlink_hijacking(tmp_path: Path, kind: str) -> None:
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


def test_result_parent_symlink_is_rejected(tmp_path: Path) -> None:
    """task-results 本身被替换为符号链接时不能创建任务目录。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    runner.runtime_root.mkdir(parents=True)
    outside = tmp_path / "outside-results"
    outside.mkdir()
    (runner.runtime_root / "task-results").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OpenCodeError) as error:
        runner.create_task_result_paths("task-1")
    assert error.value.code == "agent_result_path_invalid"


def test_cleanup_preserves_active_task_directories(tmp_path: Path) -> None:
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


def test_host_cancel_only_terminates_matching_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """host 回滚取消只终止指定 task 的本地进程，不影响其它任务。"""
    runner = OpenCodeRunner(make_settings(tmp_path))
    first = object()
    second = object()
    runner._process_tasks[first] = "task-one"  # type: ignore[index]
    runner._process_tasks[second] = "task-two"  # type: ignore[index]
    terminated: list[object] = []
    monkeypatch.setattr(runner, "_terminate", lambda process: terminated.append(process))
    runner.cancel("task-one")
    assert terminated == [first]
