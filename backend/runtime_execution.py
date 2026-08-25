"""任务与外部运行时执行的无状态公共合同。

本模块位于任务控制面与 OpenCode/executor 适配器之间，只保存可验证的任务、
scope、workspace、图片版本和 attempt 绑定事实。它不访问数据库、文件系统或
网络，便于 API、Worker、host runner 和容器 executor 共用同一套 fencing 语义。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Mapping


OPAQUE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
ACTIVE_TASK_STATUSES = frozenset({"queued", "running"})
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "unknown_execution"})
EXTERNAL_UNKNOWN_ERRORS = frozenset(
    {
        "unknown_execution",
        "agent_connection_interrupted",
        "agent_executor_unavailable",
        "agent_runtime_unavailable",
    }
)


class ExecutionBindingError(ValueError):
    """执行绑定不可信时抛出的稳定错误。

    ``code`` 只包含可对外记录的错误标识；物理路径、token 和原始 payload 不会
    通过异常消息传播到公共任务状态。
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        """创建绑定错误，供入口将其映射到现有错误协议。"""
        self.code = code
        super().__init__(message or code)


def normalize_identifier(value: object, *, kind: str) -> str:
    """规范化 task/attempt/session/scope 等不透明标识。

    输入成功时返回原字符串；类型、长度或控制字符不符合协议时抛出
    ``ExecutionBindingError``，调用方可在请求边界将其转换为稳定的 mismatch 错误。
    """
    if not isinstance(value, str) or not value or len(value) > 255 or OPAQUE_IDENTIFIER_RE.fullmatch(value) is None:
        raise ExecutionBindingError(f"{kind}_invalid")
    return value


def utc_timestamp() -> str:
    """返回 attempt 诊断使用的 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


def stable_input_digest(*values: object) -> str:
    """为外部 attempt 输入生成稳定摘要，不把 secret 或完整 prompt 写入状态。"""
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExecutionBinding:
    """一次外部执行可接受的身份边界。

    ``claim_generation`` 是数据库 lease fencing 代数；``workspace_selector`` 和
    ``image_sha256`` 是可选业务事实，存在时必须在结果采纳前再次匹配。
    """

    task_id: str
    attempt_id: str
    scope_id: str
    claim_generation: int | None = None
    image_sha256: str | None = None
    workspace_selector: str | None = None
    input_digest: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        """校验构造时的身份字段，避免无效值进入运行时 adapter。"""
        for value, kind in ((self.task_id, "task"), (self.attempt_id, "attempt"), (self.scope_id, "scope")):
            normalize_identifier(value, kind=kind)
        if self.claim_generation is not None and (not isinstance(self.claim_generation, int) or isinstance(self.claim_generation, bool) or self.claim_generation < 0):
            raise ExecutionBindingError("claim_generation_invalid")
        if self.image_sha256 is not None and (not isinstance(self.image_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", self.image_sha256)):
            raise ExecutionBindingError("image_version_invalid")
        if self.workspace_selector is not None:
            normalize_identifier(self.workspace_selector, kind="workspace")
        if self.input_digest is not None and re.fullmatch(r"[0-9a-fA-F]{64}", self.input_digest) is None:
            raise ExecutionBindingError("input_digest_invalid")
        if self.session_id is not None:
            normalize_identifier(self.session_id, kind="session")

    def matches(self, values: Mapping[str, object], *, require_attempt: bool = True) -> bool:
        """判断可信映射是否与当前绑定一致。

        ``values`` 通常来自数据库 task/attempt 或 executor 响应；缺少必需字段
        会返回 False，避免把不完整响应当作成功。
        """
        expected = {
            "task_id": self.task_id,
            "scope_id": self.scope_id,
            "claim_generation": self.claim_generation,
            "image_sha256": self.image_sha256,
            "workspace_selector": self.workspace_selector,
            "input_digest": self.input_digest,
            "session_id": self.session_id,
        }
        if require_attempt:
            expected["attempt_id"] = self.attempt_id
        for key, wanted in expected.items():
            if wanted is None:
                continue
            if values.get(key) != wanted:
                return False
        return True

    def require_matches(self, values: Mapping[str, object], *, require_attempt: bool = True) -> None:
        """要求可信映射与当前绑定完全一致，否则抛出稳定 fencing 错误。"""
        if not self.matches(values, require_attempt=require_attempt):
            raise ExecutionBindingError("execution_binding_mismatch")


@dataclass(frozen=True)
class ExecutionAttempt:
    """逻辑任务的一次实际执行尝试及其有限终态诊断。

    该值对象不负责启动进程；runtime supervisor 只需在外部副作用前后调用
    ``started``/``succeed``/``failed``/``unknown``，即可统一表达恢复和取消边界。
    """

    binding: ExecutionBinding
    status: str = "queued"
    external_effect_started: bool = False
    error_code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        """限制 attempt 状态和错误字段，防止构造出不可收束记录。"""
        if self.status not in {"queued", "running", "succeeded", "failed", "cancelled", "unknown_execution"}:
            raise ExecutionBindingError("attempt_status_invalid")
        if self.status in TERMINAL_TASK_STATUSES and self.completed_at is None:
            raise ExecutionBindingError("attempt_completion_timestamp_required")
        if self.status == "running" and self.started_at is None:
            raise ExecutionBindingError("attempt_start_timestamp_required")

    @property
    def terminal(self) -> bool:
        """返回 attempt 是否已进入不可继续执行的终态。"""
        return self.status in TERMINAL_TASK_STATUSES

    def started(self, *, at: str | None = None) -> "ExecutionAttempt":
        """标记外部副作用已开始，供 supervisor 启动进程或远端请求后调用。"""
        if self.terminal:
            raise ExecutionBindingError("attempt_already_terminal")
        return replace(self, status="running", external_effect_started=True, started_at=at or utc_timestamp())

    def succeed(self, *, at: str | None = None) -> "ExecutionAttempt":
        """在结果已绑定并完整校验后收束成功。"""
        if self.status != "running":
            raise ExecutionBindingError("attempt_not_running")
        return replace(self, status="succeeded", completed_at=at or utc_timestamp(), error_code=None)

    def failed(self, code: str, *, at: str | None = None) -> "ExecutionAttempt":
        """收束可证明的失败；外部状态不确定时应调用 ``unknown``。"""
        if self.terminal:
            raise ExecutionBindingError("attempt_already_terminal")
        if not isinstance(code, str) or not code:
            raise ExecutionBindingError("attempt_error_invalid")
        return replace(self, status="failed", completed_at=at or utc_timestamp(), error_code=code)

    def cancelled(self, *, at: str | None = None) -> "ExecutionAttempt":
        """收束用户取消或受控终止的 attempt。"""
        if self.terminal:
            raise ExecutionBindingError("attempt_already_terminal")
        return replace(self, status="cancelled", completed_at=at or utc_timestamp(), error_code="task_interrupted")

    def unknown(self, code: str = "unknown_execution", *, at: str | None = None) -> "ExecutionAttempt":
        """收束无法证明外部副作用状态的 attempt，禁止隐式重放。"""
        if self.terminal:
            raise ExecutionBindingError("attempt_already_terminal")
        _ = code if code in EXTERNAL_UNKNOWN_ERRORS else "unknown_execution"
        return replace(self, status="unknown_execution", completed_at=at or utc_timestamp(), error_code="unknown_execution")


class AttemptFence:
    """进程内的 attempt/claim fencing 辅助器。

    数据库 claim 仍是跨进程权威；本类用于在一个 adapter 内快速拒绝取消后或
    新 attempt 覆盖后的旧回调，避免旧线程继续提交结果。
    """

    def __init__(self) -> None:
        """创建空 fencing 表。"""
        self._lock = Lock()
        self._bindings: dict[str, ExecutionBinding] = {}

    def bind(self, binding: ExecutionBinding) -> None:
        """登记 task 当前绑定；替换旧 attempt 时使旧回调失效。"""
        with self._lock:
            self._bindings[binding.task_id] = binding

    def accepts(self, binding: ExecutionBinding) -> bool:
        """返回 task 当前是否仍接受该 attempt 的写回。"""
        with self._lock:
            return self._bindings.get(binding.task_id) == binding

    def release(self, task_id: str, attempt_id: str | None = None) -> None:
        """按 task/attempt 清理本地绑定，避免旧任务占用内存。"""
        with self._lock:
            current = self._bindings.get(task_id)
            if current is not None and (attempt_id is None or current.attempt_id == attempt_id):
                self._bindings.pop(task_id, None)


def classify_external_failure(code: str, *, effect_started: bool, status_known: bool) -> str:
    """把 runtime 失败分类为可重试失败或 fail-closed unknown execution。"""
    if not status_known or (effect_started and code in EXTERNAL_UNKNOWN_ERRORS):
        return "unknown_execution"
    return code if isinstance(code, str) and code else "task_failed"


__all__ = [
    "ACTIVE_TASK_STATUSES",
    "AttemptFence",
    "ExecutionAttempt",
    "ExecutionBinding",
    "ExecutionBindingError",
    "EXTERNAL_UNKNOWN_ERRORS",
    "TERMINAL_TASK_STATUSES",
    "classify_external_failure",
    "normalize_identifier",
    "stable_input_digest",
    "utc_timestamp",
]
