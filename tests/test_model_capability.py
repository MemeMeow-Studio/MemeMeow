"""模型 broker endpoint 与短期 capability 公共协议测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.model_capability import ModelCapabilityError, validate_model_broker_url, validate_model_capability
from executor import server as executor_server


def test_model_capability_rejects_url_credentials_and_query() -> None:
    """broker 地址不得携带用户信息、查询参数或片段。"""
    for value in (
        "https://user:password@example.invalid/v1",
        "https://broker.example/v1?redirect=https://other.invalid",
        "https://broker.example/v1#fragment",
        "file:///tmp/broker",
    ):
        with pytest.raises(ModelCapabilityError):
            validate_model_broker_url(value)


def test_model_capability_rejects_empty_control_and_oversized_values() -> None:
    """短期 capability 只接受有界、无空白控制字符的 opaque 值。"""
    for value in ("", "too-short", "capability with-space", "x" * 8193):
        with pytest.raises(ModelCapabilityError):
            validate_model_capability(value)
    assert validate_model_capability("capability-" + "x" * 20).startswith("capability-")


def test_executor_production_ignores_legacy_long_term_model_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """生产 executor 即使残留旧模型 key，也必须因缺少 broker 而不可用。"""
    runtime = tmp_path / "runtime"
    images = tmp_path / "images"
    skills = tmp_path / "skills"
    for path in (runtime, images, skills):
        path.mkdir()
    (images / "sample.png").write_bytes(b"image")
    monkeypatch.setattr(executor_server, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(executor_server, "WORKSPACE", runtime / "workspace")
    monkeypatch.setattr(executor_server, "RESULT_ROOT", runtime / "task-results")
    monkeypatch.setattr(executor_server, "LOG_ROOT", runtime / "logs")
    monkeypatch.setattr(executor_server, "IMAGE_ROOT", images)
    monkeypatch.setattr(executor_server, "SKILL_ROOT", skills)
    monkeypatch.setenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "executor-token")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_MODEL", "mememeow/gpt-5.6-luna")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_BASE_URL", "https://legacy.invalid/v1")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_API_KEY", "legacy-secret")
    monkeypatch.setenv("MEMEMEOW_PUBLIC_RELEASE_PROFILE", "production")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_EXECUTABLE", "/missing/opencode")
    executor = executor_server.Executor()
    try:
        assert executor.model_configured is False
        assert executor.health()["model_broker_configured"] is False
        assert executor._task_environment(
            executor_server.TaskState(
                task_id="task",
                business_task_id="task",
                executor_attempt_id="attempt",
                image_relative_path="sample.png",
                reverse_image_policy="forbid",
                timeout_seconds=10,
            )
        ).get("MEMEMEOW_OPENCODE_API_KEY") is None
    finally:
        executor.close()


def test_runtime_config_uses_broker_capability_names() -> None:
    """Agent 镜像的 OpenCode 配置只能引用 broker 与短期 capability。"""
    source = Path("executor/runtime_opencode_config.py").read_text(encoding="utf-8")
    document = source
    assert "MEMEMEOW_MODEL_BROKER_URL" in document
    assert "MEMEMEOW_MODEL_CAPABILITY" in document
    assert "MEMEMEOW_OPENCODE_API_KEY" not in document
    json.dumps(document)
