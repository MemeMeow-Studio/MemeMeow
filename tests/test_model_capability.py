"""模型 broker endpoint 与短期 capability 公共协议测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.model_capability import ModelCapabilityError, validate_model_broker_url, validate_model_capability
from backend.model_capability_provider import (
    ModelCapabilityProviderError,
    ModelCapabilityRequest,
    capability_for_model_provider,
    normalize_observed_cost,
)
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


class _RecordingCapabilityProvider:
    """记录签发请求字段，验证 capability 绑定的 attempt 和模型事实。"""

    def __init__(self) -> None:
        self.requests: list[ModelCapabilityRequest] = []

    def capability(self, request: ModelCapabilityRequest) -> str:
        """返回满足公共传输约束的测试 capability。"""
        self.requests.append(request)
        return "capability-" + "x" * 20


def test_model_capability_provider_receives_frozen_attempt_facts() -> None:
    """provider 必须收到当前任务、attempt、模型、策略和恢复金额。"""
    provider = _RecordingCapabilityProvider()
    request = ModelCapabilityRequest(
        task_id="task-1",
        attempt_id="attempt-1",
        scope_id="scope-1",
        model="mememeow/gpt-5.6-sol",
        variant="max",
        session_id="session-1",
        resume_of_attempt_id="attempt-0",
        analysis_policy={"version": 1, "model_key": "model_plus", "termination_cost": "0.30"},
        observed_cost=normalize_observed_cost("0.28"),
    )

    capability = capability_for_model_provider(provider, request)

    assert capability.startswith("capability-")
    assert provider.requests == [request]


@pytest.mark.parametrize("value", [True, "NaN", "-0.1"])
def test_model_capability_provider_rejects_invalid_recovery_cost(value: object) -> None:
    """恢复金额不能通过 capability 请求进入 broker。"""
    with pytest.raises(ModelCapabilityProviderError, match="恢复金额无效"):
        normalize_observed_cost(value)


def test_model_capability_provider_rejects_invalid_provider_result() -> None:
    """provider 返回越界 capability 时必须保留稳定错误。"""
    class InvalidProvider:
        """返回无效值的 provider 测试实现。"""

        def capability(self, _request: ModelCapabilityRequest) -> str:
            """返回过短值。"""
            return "short"

    with pytest.raises(ModelCapabilityProviderError, match="模型 capability 无效"):
        capability_for_model_provider(InvalidProvider(), ModelCapabilityRequest(
            task_id="task-1", attempt_id="attempt-1", scope_id="scope-1",
            model="mememeow/gpt-5.6-sol", variant="max", session_id=None,
            resume_of_attempt_id=None, analysis_policy=None, observed_cost=None,
        ))
