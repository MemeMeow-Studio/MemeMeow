"""Agent session 续跑的错误分类、退避和诊断脱敏测试。"""

from __future__ import annotations

from backend.agent_resume import append_error_history, agent_failure_requires_unknown, bounded_backoff, classify_resume_error, normalize_config_hash, sanitize_error


def test_resume_error_matrix_fails_closed_without_session_or_target_identity() -> None:
    """没有明确 session、输入变化或未知执行时不得自动重放。"""
    assert classify_resume_error("agent_provider_rate_limited", session_id=None).reason == "session_missing"
    assert not classify_resume_error("agent_provider_server_error", session_id="session-1", target_unchanged=False).available
    assert classify_resume_error("unknown_execution", session_id="session-1").reason == "unknown_execution"
    assert classify_resume_error("agent_provider_rate_limited", session_id="session-1").available


def test_resume_backoff_and_error_history_are_bounded_and_deduplicated() -> None:
    """退避受上限限制，重复写回同一 attempt 不覆盖首次诊断。"""
    assert bounded_backoff(0, base_seconds=2, max_seconds=5) == 2
    assert bounded_backoff(4, base_seconds=2, max_seconds=5) == 5
    assert bounded_backoff(0, base_seconds=2, max_seconds=0) == 0
    assert bounded_backoff(0, base_seconds=5, max_seconds=2) == 2
    history = append_error_history([], {"error": "agent_provider_rate_limited", "message": "secret=hidden"}, attempt=1, executor_attempt_id="attempt-1", session_id="session-1")
    history = append_error_history(history, {"error": "agent_provider_rate_limited", "message": "later"}, attempt=1, executor_attempt_id="attempt-1", session_id="session-1")
    assert len(history) == 1
    assert sanitize_error({"error": "provider", "message": "x" * 1000, "http_status": 429})["message"] == "x" * 500
    assert "reason_code" not in sanitize_error({"error": "provider", "message": "x", "reason_code": "result_sensitive_data"})
    assert sanitize_error(
        {"error": "agent_result_file_schema_invalid", "message": "任务执行失败", "reason_code": "result_sensitive_data"},
        include_reason_code=True,
    )["reason_code"] == "result_sensitive_data"


def test_error_sanitization_redacts_json_secrets_and_generic_host_paths() -> None:
    """任务诊断不能通过 JSON 凭据或宿主绝对路径泄漏内部信息。"""
    message = '{"api_key":"json-secret","token":"json-token","path":"/home/alice/project/result.json"}'
    redacted = sanitize_error({"error": "agent_process_failed", "message": message})["message"]
    assert "json-secret" not in redacted
    assert "json-token" not in redacted
    assert "/home/alice/project/result.json" not in redacted
    assert "[REDACTED]" in redacted
    assert "[PATH]" in redacted


def test_resume_config_hash_is_normalized_and_rejects_non_sha_values() -> None:
    """恢复绑定只接受 64 位配置摘要，并统一大小写表示。"""
    assert normalize_config_hash("A" * 64) == "a" * 64
    assert normalize_config_hash("not-a-config-hash") is None


def test_external_agent_failures_do_not_fall_back_to_task_replay() -> None:
    """图片 Agent 的超时/坏响应和无 session 连接故障必须进入未知执行。"""
    assert agent_failure_requires_unknown("agent_timeout", session_id="session-1", resume_available=False, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_executor_invalid_response", session_id=None, resume_available=False, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_connection_interrupted", session_id=None, resume_available=False, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_provider_rate_limited", session_id=None, resume_available=False, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_provider_server_error", session_id=None, resume_available=False, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_process_failed", session_id=None, resume_available=False, resume_enabled=True)
    assert not agent_failure_requires_unknown("agent_connection_interrupted", session_id="session-1", resume_available=True, resume_enabled=True)
    assert agent_failure_requires_unknown("task_exists", session_id="session-1", resume_available=False, resuming=True, resume_enabled=True)
    assert agent_failure_requires_unknown("session_not_resumable", session_id="session-1", resume_available=False, resuming=True, resume_enabled=True)
    assert agent_failure_requires_unknown("agent_provider_rate_limited", session_id="session-1", resume_available=False, resume_enabled=True)
    assert not agent_failure_requires_unknown("agent_provider_rate_limited", session_id="session-1", resume_available=True, resume_enabled=True)


def test_resume_disabled_preserves_task_level_retry_for_provider_and_process_errors() -> None:
    """续跑开关关闭时，429/5xx/进程失败仍保留原任务级重试语义。"""
    for code in ("agent_provider_server_error", "agent_process_failed"):
        assert not agent_failure_requires_unknown(code, session_id=None, resume_available=False, resume_enabled=False)
        assert agent_failure_requires_unknown(code, session_id=None, resume_available=False, resume_enabled=True)
