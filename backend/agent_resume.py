"""OpenCode 会话续跑的错误分类、退避和脱敏契约。

该模块位于 executor、Runner 与 PostgreSQL Worker 之间，集中定义哪些错误可以
在明确 session 绑定下续跑，避免三个执行层各自维护一套不一致的恢复规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


UTC = timezone.utc
MAX_ERROR_MESSAGE = 500
MAX_ERROR_HISTORY = 8
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
ATTEMPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
CONFIG_HASH_RE = re.compile(r"[0-9A-Fa-f]{64}\Z")
_SECRET_PATTERNS = (
    # 分开处理 JSON 字符串值和普通 header/日志格式，保留原有引号结构。
    re.compile(r"(?i)(authorization\s*[\"']?\s*:\s*[\"']?bearer\s+)[^\s,;}\]\"']+"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*:\s*)([\"'])(.*?)\2"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*[:=]\s*)[^\s,;}\]\"']+"),
)
_PATH_PATTERN = re.compile(r"(?:/runtime|/images|/skills|/app|[A-Za-z]:\\)[^\s,;]+")
_GENERIC_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s,;:'\"()\[\]{}]+/)+[^\s,;:'\"()\[\]{}]+")

# 这些错误只描述模型网关、网络或 executor 进程级暂态故障；业务 callback、
# 计量和结果写回一旦进入不确定状态，必须走 unknown_execution 而不能重放。
RESUME_RETRYABLE_ERRORS = frozenset(
    {
        "agent_provider_rate_limited",
        "agent_provider_server_error",
        "agent_connection_interrupted",
        "agent_process_failed",
        "agent_executor_unavailable",
    }
)
RESUME_UNSAFE_ERRORS = frozenset(
    {
        "unknown_execution",
        "reverse_image_unknown_execution",
        "auto_rename_unknown_execution",
        "target_changed",
        "agent_output_invalid_json",
        "agent_output_schema_invalid",
        "agent_result_file_missing",
        "agent_result_file_unreadable",
        "agent_result_file_too_large",
        "agent_result_file_invalid_json",
        "agent_result_file_schema_invalid",
        "operation_grant_invalid",
        "operation_policy_unavailable",
        "agent_callback_invalid_execution",
    }
)
# 这些错误发生在图片 Agent 已进入外部执行窗口后，若没有可验证的 session，
# 不能退化为从头重放；否则可能重复尚未确认的工具或模型调用。
AGENT_EXTERNAL_UNKNOWN_ERRORS = frozenset(
    {
        "agent_timeout",
        "agent_executor_invalid_response",
        "session_binding_mismatch",
        "session_not_resumable",
    }
)
AGENT_SESSION_BOUND_ERRORS = frozenset(
    {
        "agent_executor_unavailable",
        "agent_connection_interrupted",
    }
)


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """一次失败是否允许 session 续跑及其公开原因。"""

    available: bool
    reason: str
    retryable: bool


def agent_failure_requires_unknown(
    code: object,
    *,
    session_id: str | None,
    resume_available: bool,
    resuming: bool = False,
    resume_enabled: bool = False,
) -> bool:
    """判断图片 Agent 失败是否必须禁止普通任务级重放。

    超时和无效 executor 响应始终无法证明外部调用边界；executor 不可用或连接
    中断只有在 handler 已保存可验证 session 并明确标记可续跑时，才允许交给续跑
    编排；续跑 attempt 的 task_exists/session 绑定错误也不能退回首次执行。该判定
    只在会话续跑开关开启时约束图片 Agent；开关关闭时普通任务仍沿用原有 retry
    语义，避免 rollout 改变既有任务级重试行为。
    """
    if not resume_enabled:
        return False
    normalized = str(code) if isinstance(code, str) else ""
    if normalized in AGENT_EXTERNAL_UNKNOWN_ERRORS:
        return True
    if resuming and normalized == "task_exists":
        return True
    # provider/进程错误同样发生在 Agent 调用边界之后；没有 session 就无法证明
    # 请求是否已经生效，不能因恢复标识缺失而退化为一次全新的外部调用。
    if normalized in RESUME_RETRYABLE_ERRORS:
        # 仅有 session 字符串还不足以证明它已经和当前失败 attempt 绑定；
        # handler 必须先把带 fencing 的 attempt 事实成功写入数据库。
        return not (resume_available and normalize_identifier(session_id, kind="session"))
    if normalized in AGENT_SESSION_BOUND_ERRORS:
        return not (resume_available and normalize_identifier(session_id, kind="session"))
    return False


def normalize_identifier(value: object, *, kind: str) -> str | None:
    """校验并返回 session/attempt 标识；不接受路径、空白或控制字符。"""
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    pattern = SESSION_ID_RE if kind == "session" else ATTEMPT_ID_RE
    return value if pattern.fullmatch(value) else None


def normalize_config_hash(value: object) -> str | None:
    """校验并返回处理配置 SHA-256；续跑绑定必须使用规范化小写值。"""
    if not isinstance(value, str) or not CONFIG_HASH_RE.fullmatch(value):
        return None
    return value.lower()


def classify_resume_error(
    code: object,
    *,
    session_id: str | None,
    external_started: bool = False,
    result_valid: bool = False,
    target_unchanged: bool = True,
    grant_state: str | None = None,
) -> ResumeDecision:
    """按错误、session 和副作用事实计算 fail-closed 的续跑资格。"""
    normalized = str(code) if isinstance(code, str) else "agent_process_failed"
    if normalized in RESUME_UNSAFE_ERRORS or not target_unchanged:
        return ResumeDecision(False, normalized if normalized in RESUME_UNSAFE_ERRORS else "target_changed", False)
    if grant_state in {"unknown", "released"} or external_started and grant_state == "unknown":
        return ResumeDecision(False, "unknown_execution", False)
    if result_valid:
        return ResumeDecision(False, "result_already_verified", False)
    if normalized not in RESUME_RETRYABLE_ERRORS:
        return ResumeDecision(False, normalized, False)
    if not normalize_identifier(session_id, kind="session"):
        return ResumeDecision(False, "session_missing", False)
    return ResumeDecision(True, normalized, True)


def bounded_backoff(attempt: int, *, base_seconds: int = 1, max_seconds: int = 60) -> int:
    """返回指数退避秒数，并把配置和 attempt 限制在安全边界内。"""
    try:
        value = max(0, int(attempt))
        base = max(0, min(int(base_seconds), 3600))
        upper = max(0, min(int(max_seconds), 3600))
    except (TypeError, ValueError):
        return 0
    if base == 0 or upper == 0:
        return 0
    return min(upper, base * (2 ** min(value, 10)))


def within_total_timeout(started_at: datetime | None, *, timeout_seconds: int, now: datetime | None = None) -> bool:
    """判断续跑累计时间是否仍在总超时窗口内。"""
    if started_at is None:
        return True
    current = now or datetime.now(UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    try:
        limit = max(1, min(int(timeout_seconds), 86400))
    except (TypeError, ValueError):
        return False
    return current - started_at <= timedelta(seconds=limit)


def sanitize_error(error: Mapping[str, Any] | None, *, fallback: str = "task_failed") -> dict[str, Any]:
    """只保留稳定错误码、短消息和 HTTP 状态，拒绝 transcript/凭据泄漏。"""
    source = error if isinstance(error, Mapping) else {}
    code = source.get("error")
    code = str(code)[:128] if isinstance(code, str) and code else fallback
    message = source.get("message")
    if isinstance(message, str):
        message = message.splitlines()[0] if message.splitlines() else code
        for pattern in _SECRET_PATTERNS:
            if pattern.groups == 3:
                message = pattern.sub(r"\1\2[REDACTED]\2", message)
            else:
                message = pattern.sub(r"\1[REDACTED]", message)
        message = _PATH_PATTERN.sub("[PATH]", message)
        message = _GENERIC_PATH_PATTERN.sub("[PATH]", message)
        message = message[:MAX_ERROR_MESSAGE]
    else:
        message = code
    result: dict[str, Any] = {"error": code, "message": message}
    status = source.get("http_status")
    if isinstance(status, int) and 100 <= status <= 599:
        result["http_status"] = status
    return result


def append_error_history(
    history: object,
    error: Mapping[str, Any] | None,
    *,
    attempt: int | None = None,
    executor_attempt_id: str | None = None,
    session_id: str | None = None,
    occurred_at: str | None = None,
) -> list[dict[str, Any]]:
    """追加有限脱敏错误历史，并保留最早错误顺序。"""
    values = sanitize_error_history(history)
    item = sanitize_error(error)
    if isinstance(attempt, int) and attempt > 0:
        item["attempt"] = attempt
    if normalize_identifier(executor_attempt_id, kind="attempt"):
        item["executor_attempt_id"] = executor_attempt_id
    if normalize_identifier(session_id, kind="session"):
        item["session_id"] = session_id
    if isinstance(occurred_at, str) and occurred_at:
        item["occurred_at"] = occurred_at[:64]
    # handler 先写入 attempt 诊断、fenced failure 随后写入任务终态时，二者
    # 可能描述同一次失败；按稳定身份去重，仍保留不同阶段的后续错误。
    if values:
        previous = values[-1]
        if (
            previous.get("error") == item.get("error")
            and previous.get("attempt") == item.get("attempt")
            and previous.get("executor_attempt_id") == item.get("executor_attempt_id")
            and previous.get("session_id") == item.get("session_id")
        ):
            return values[-MAX_ERROR_HISTORY:]
    values.append(item)
    return values[-MAX_ERROR_HISTORY:]


def append_task_error_history(
    task: Any,
    error: Mapping[str, Any] | None,
    *,
    attempt: int | None = None,
    executor_attempt_id: str | None = None,
    session_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """向任务追加脱敏错误，并在已有主错误时保留首次诊断。"""
    safe_error = sanitize_error(error)
    current = getattr(task, "error", None)
    if not isinstance(getattr(task, "first_error", None), dict):
        task.first_error = sanitize_error(current) if isinstance(current, dict) else safe_error
    task.error_history = append_error_history(
        getattr(task, "error_history", None),
        safe_error,
        attempt=attempt,
        executor_attempt_id=executor_attempt_id,
        session_id=session_id,
        occurred_at=occurred_at,
    )
    return safe_error


def sanitize_error_history(history: object) -> list[dict[str, Any]]:
    """读取历史错误时重新收窄字段，防止旧行携带任意诊断键。"""
    values: list[dict[str, Any]] = []
    if isinstance(history, list):
        for raw in history:
            if not isinstance(raw, Mapping):
                continue
            item = sanitize_error(raw)
            if isinstance(raw.get("attempt"), int) and raw["attempt"] > 0:
                item["attempt"] = raw["attempt"]
            if normalize_identifier(raw.get("executor_attempt_id"), kind="attempt"):
                item["executor_attempt_id"] = raw["executor_attempt_id"]
            if normalize_identifier(raw.get("session_id"), kind="session"):
                item["session_id"] = raw["session_id"]
            if isinstance(raw.get("occurred_at"), str) and raw["occurred_at"]:
                item["occurred_at"] = raw["occurred_at"][:64]
            values.append(item)
    return values[-MAX_ERROR_HISTORY:]
