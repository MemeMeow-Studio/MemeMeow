"""Compose 内部 Agent executor 的最小 HTTP 客户端。

API 容器只依赖该模块的结构化请求，不导入 Docker SDK、调用 Docker CLI，也不
接触宿主 Docker socket。executor 负责 OpenCode 子进程和共享结果目录；本模块
负责凭据校验、超时、取消及稳定错误码映射。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from uuid import uuid4
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from executor.model_capability import MODEL_CAPABILITY_FIELD, ModelCapabilityError, validate_model_capability


class AgentExecutorError(RuntimeError):
    """executor 请求失败，携带稳定错误码和安全诊断。"""

    def __init__(self, code: str, message: str | None = None, *, session_id: str | None = None, executor_attempt_id: str | None = None, http_status: int | None = None, reason_code: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.session_id = session_id
        self.executor_attempt_id = executor_attempt_id
        self.http_status = http_status
        self.reason_code = reason_code if isinstance(reason_code, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", reason_code) else None


_PENDING_STATUSES = frozenset({"queued", "running"})
_ATTEMPT_HISTORY_LIMIT = 5000
_KNOWN_TASK_ERRORS = frozenset(
    {
        "agent_timeout",
        "task_interrupted",
        "agent_process_failed",
        "unknown_execution",
        "agent_output_invalid_json",
        "agent_result_file_missing",
        "agent_result_file_unreadable",
        "agent_result_file_too_large",
        "agent_result_file_invalid_json",
        "agent_result_file_schema_invalid",
        "agent_result_path_invalid",
        "agent_image_path_forbidden",
        "agent_timeout_limit_exceeded",
        "agent_runtime_unavailable",
        "opencode_not_configured",
        "invalid_task",
        "invalid_reverse_image_policy",
        "agent_backpressure",
        "task_exists",
        "agent_provider_rate_limited",
        "agent_provider_server_error",
        "agent_connection_interrupted",
        "session_binding_mismatch",
        "session_not_resumable",
        "opencode_workspace_invalid",
        "opencode_workspace_mismatch",
        "opencode_workspace_capability_invalid",
        "opencode_workspace_capability_expired",
        "opencode_workspace_capability_unavailable",
        "opencode_workspace_provider_missing",
        "model_capability_invalid",
        "model_capability_unavailable",
        "model_broker_endpoint_invalid",
    }
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """executor 内部请求禁止跟随跳转，避免把 Bearer token 转发到其它主机。"""

    def redirect_request(self, *_args: Any, **_kwargs: Any):
        """拒绝所有 HTTP 重定向。"""
        return None


_DEFAULT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)


@dataclass(frozen=True)
class ExecutorTaskResponse:
    """executor 返回的有限任务状态。"""

    task_id: str
    status: str
    session_id: str | None
    error: dict[str, str] | None
    result_path: str | None
    executor_attempt_id: str | None = None
    business_task_id: str | None = None


class AgentExecutorClient:
    """调用固定 executor 任务协议的同步客户端。"""

    def __init__(self, url: str | None, token: str | None, *, opener: Callable[..., Any] | None = None, timeout: int = 1810):
        """保存内部地址和 token；token 只存在内存，不写入日志或结果文件。"""
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.opener = opener or _DEFAULT_OPENER.open
        self.timeout = max(1, int(timeout))
        self._attempt_ids: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        """判断 URL 和不可为空 token 是否同时存在且 URL 是 HTTP(S)。"""
        if not self.url or not self.token:
            return False
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.query and not parsed.fragment

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None, *, timeout: int | None = None) -> tuple[int, dict[str, object]]:
        """发送 JSON 请求并将 HTTP/JSON 故障映射为稳定错误。"""
        if not self.configured:
            raise AgentExecutorError("agent_executor_not_configured", "Agent executor 地址或凭据 token 未配置")
        body = None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.url}{path}", data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=timeout or self.timeout) as response:
                response_status = getattr(response, "status", None)
                if response_status is None:
                    response_status = response.getcode()
                status = int(response_status)
                raw = response.read(128 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(128 * 1024)
                payload_value = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload_value = {}
            code = payload_value.get("error") if isinstance(payload_value, dict) else None
            message = payload_value.get("message") if isinstance(payload_value, dict) else None
            reason_code = payload_value.get("reason_code") if isinstance(payload_value, dict) else None
            if not isinstance(code, str):
                code = "agent_executor_http_error"
            if exc.code in {401, 403}:
                code = "agent_executor_unauthorized"
            raise AgentExecutorError(code, str(message or "Agent executor 请求失败")[:500], http_status=exc.code, reason_code=reason_code) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                raise AgentExecutorError("agent_timeout", "Agent executor 请求超时") from exc
            raise AgentExecutorError("agent_executor_unavailable", "Agent executor 暂时不可用") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AgentExecutorError("agent_timeout", "Agent executor 请求超时") from exc
        except OSError as exc:
            raise AgentExecutorError("agent_executor_unavailable", "Agent executor 暂时不可用") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 返回格式无效") from exc
        if not isinstance(value, dict):
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 返回格式无效")
        return status, value

    @staticmethod
    def _response(value: dict[str, object]) -> ExecutorTaskResponse:
        """将服务响应压缩为不包含任意字段的任务状态对象。"""
        task_id = value.get("task_id")
        status = value.get("status")
        if not isinstance(task_id, str) or not isinstance(status, str):
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 任务响应缺少字段")
        error = value.get("error")
        if not isinstance(error, dict):
            error = None
        return ExecutorTaskResponse(
            task_id=task_id,
            status=status,
            session_id=value.get("session_id") if isinstance(value.get("session_id"), str) else None,
            error={str(key): str(item) for key, item in error.items()} if error else None,
            result_path=value.get("result_path") if isinstance(value.get("result_path"), str) else None,
            executor_attempt_id=value.get("executor_attempt_id") if isinstance(value.get("executor_attempt_id"), str) else None,
            business_task_id=value.get("business_task_id") if isinstance(value.get("business_task_id"), str) else None,
        )

    @staticmethod
    def _for_task(response: ExecutorTaskResponse, task_id: str) -> ExecutorTaskResponse:
        """确认响应仍绑定原始任务，避免代理或服务错误串接其它任务状态。"""
        if response.task_id != task_id:
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 任务标识不匹配")
        if response.business_task_id is not None and response.business_task_id != task_id:
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 业务任务标识不匹配")
        return response

    @staticmethod
    def _for_executor_attempt(response: ExecutorTaskResponse, executor_attempt_id: str) -> ExecutorTaskResponse:
        """确认按 attempt 路径查询的响应没有串接到其它 executor 任务。"""
        if response.executor_attempt_id:
            if response.executor_attempt_id != executor_attempt_id:
                raise AgentExecutorError("agent_executor_invalid_response", "Agent executor attempt 标识不匹配")
        elif response.task_id != executor_attempt_id:
            # 旧服务只支持业务 task 路径；只有没有新字段时才允许该兼容分支。
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 任务标识不匹配")
        return response

    @staticmethod
    def _failure_code(response: ExecutorTaskResponse) -> str:
        """把 executor 返回的失败码限制在后端可持久化的稳定集合内。"""
        code = (response.error or {}).get("error")
        return code if code in _KNOWN_TASK_ERRORS else "agent_process_failed"

    def _wait_for_terminal(self, response: ExecutorTaskResponse, *, task_id: str, executor_task_id: str, timeout_seconds: int) -> ExecutorTaskResponse:
        """轮询同步提交的非终态响应，超时后只取消当前任务。"""
        deadline = time.monotonic() + max(5, int(timeout_seconds) + 10)
        poll_delay = 0.2
        while response.status in _PENDING_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.cancel(executor_task_id)
                except AgentExecutorError:
                    pass
                raise AgentExecutorError("agent_timeout", "Agent executor 等待任务终态超时")
            time.sleep(min(poll_delay, remaining))
            poll_delay = min(2.0, poll_delay * 1.5)
            try:
                response = self._for_task(self.status(executor_task_id), task_id)
            except AgentExecutorError:
                # 轮询链路断开时尽力取消已提交任务，防止 HTTP 响应丢失后孤儿执行。
                try:
                    self.cancel(executor_task_id)
                except AgentExecutorError:
                    pass
                raise
        return response

    def health(self) -> dict[str, object]:
        """读取 executor 健康状态并隐藏响应中的未知字段。"""
        _status, value = self._request("GET", "/health", timeout=min(10, self.timeout))
        return {
            key: value[key]
            for key in (
                "status",
                "ready",
                "executor",
                "opencode",
                "runtime_read_write",
                "images_read_only",
                "skills_read_only",
                "docker_socket_absent",
                "token_configured",
                "capacity",
                "queued",
            )
            if key in value
        }

    def attempt_id_for(self, task_id: str) -> str | None:
        """返回当前业务任务最近一次提交的 executor attempt 标识。"""
        return self._attempt_ids.get(task_id)

    def run(
        self,
        *,
        task_id: str,
        image_relative_path: str,
        reverse_image_policy: str,
        timeout_seconds: int,
        callback_token: str | None = None,
        executor_attempt_id: str | None = None,
        session_id: str | None = None,
        resume_of_attempt_id: str | None = None,
        processing_config_hash: str | None = None,
        workspace_selector: str | None = None,
        workspace_capability: str | None = None,
        model_capability: str | None = None,
    ) -> ExecutorTaskResponse:
        """提交绑定模型 capability 的独立 executor attempt，并可按明确 session 续跑。"""
        timeout_value = int(timeout_seconds)
        attempt_id = executor_attempt_id or f"attempt-{uuid4().hex}"
        if model_capability is not None:
            try:
                model_capability = validate_model_capability(model_capability)
            except ModelCapabilityError as exc:
                raise AgentExecutorError(str(exc), "模型 capability 无效", executor_attempt_id=attempt_id) from exc
        self._attempt_ids[task_id] = attempt_id
        # 该映射只服务于取消和诊断；限制历史长度避免长期 API 进程被业务 task ID 耗尽内存。
        while len(self._attempt_ids) > _ATTEMPT_HISTORY_LIMIT:
            self._attempt_ids.pop(next(iter(self._attempt_ids)))
        try:
            _status, value = self._request(
                "POST",
                "/v1/tasks",
                {
                    "task_id": task_id,
                    "business_task_id": task_id,
                    "executor_attempt_id": attempt_id,
                    "image_relative_path": image_relative_path,
                    "reverse_image_policy": reverse_image_policy,
                    "timeout_seconds": timeout_value,
                    "wait": True,
                    **({"session_id": session_id} if session_id else {}),
                    **({"resume_of_attempt_id": resume_of_attempt_id} if resume_of_attempt_id else {}),
                    **({"processing_config_hash": processing_config_hash} if processing_config_hash else {}),
                    **({"workspace_selector": workspace_selector} if workspace_selector else {}),
                    **({"workspace_capability": workspace_capability} if workspace_capability else {}),
                    **({MODEL_CAPABILITY_FIELD: model_capability} if model_capability else {}),
                    **({"callback_token": callback_token} if callback_token else {}),
                },
                timeout=max(self.timeout, timeout_value + 10),
            )
        except AgentExecutorError as exc:
            raise AgentExecutorError(
                exc.code,
                str(exc)[:500],
                session_id=exc.session_id,
                executor_attempt_id=exc.executor_attempt_id or attempt_id,
                http_status=exc.http_status,
                reason_code=exc.reason_code,
            ) from exc
        response = self._for_task(self._response(value), task_id)
        if response.executor_attempt_id and response.executor_attempt_id != attempt_id:
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor attempt 标识不匹配", executor_attempt_id=attempt_id)
        # 旧 executor 响应没有独立 attempt 字段时沿用业务 task 路径，保持升级期间
        # 的轮询兼容；新协议总会返回显式 executor_attempt_id。
        executor_task_id = response.executor_attempt_id or task_id
        try:
            response = self._wait_for_terminal(response, task_id=task_id, executor_task_id=executor_task_id, timeout_seconds=timeout_value)
        except AgentExecutorError as exc:
            raise AgentExecutorError(
                exc.code,
                str(exc)[:500],
                session_id=exc.session_id or response.session_id,
                executor_attempt_id=exc.executor_attempt_id or response.executor_attempt_id or attempt_id,
                http_status=exc.http_status,
                reason_code=exc.reason_code,
            ) from exc
        if response.status == "failed":
            code = self._failure_code(response)
            error = response.error or {}
            raw_status = error.get("http_status")
            http_status = int(raw_status) if isinstance(raw_status, int) or (isinstance(raw_status, str) and raw_status.isdigit()) else None
            raise AgentExecutorError(
                code,
                str(error.get("message") or code)[:500],
                session_id=response.session_id,
                executor_attempt_id=response.executor_attempt_id or attempt_id,
                http_status=http_status,
                reason_code=error.get("reason_code"),
            )
        if response.status == "cancelled":
            raise AgentExecutorError("task_interrupted", "Agent 任务已取消", session_id=response.session_id, executor_attempt_id=response.executor_attempt_id or attempt_id)
        if response.status != "succeeded":
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 任务状态无效")
        return response

    def cancel(self, task_id: str) -> ExecutorTaskResponse:
        """取消指定任务，超时或服务关闭时调用。"""
        _status, value = self._request("POST", f"/v1/tasks/{quote(task_id, safe='')}/cancel", timeout=min(10, self.timeout))
        return self._for_executor_attempt(self._response(value), task_id)

    def status(self, task_id: str) -> ExecutorTaskResponse:
        """读取指定任务状态，用于诊断和异步调用方。"""
        _status, value = self._request("GET", f"/v1/tasks/{quote(task_id, safe='')}", timeout=min(10, self.timeout))
        return self._for_executor_attempt(self._response(value), task_id)
