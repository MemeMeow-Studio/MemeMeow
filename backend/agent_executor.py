"""Compose 内部 Agent executor 的最小 HTTP 客户端。

API 容器只依赖该模块的结构化请求，不导入 Docker SDK、调用 Docker CLI，也不
接触宿主 Docker socket。executor 负责 OpenCode 子进程和共享结果目录；本模块
负责凭据校验、超时、取消及稳定错误码映射。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class AgentExecutorError(RuntimeError):
    """executor 请求失败，携带稳定错误码和安全诊断。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ExecutorTaskResponse:
    """executor 返回的有限任务状态。"""

    task_id: str
    status: str
    session_id: str | None
    error: dict[str, str] | None
    result_path: str | None


class AgentExecutorClient:
    """调用固定 executor 任务协议的同步客户端。"""

    def __init__(self, url: str | None, token: str | None, *, opener: Callable[..., Any] | None = None, timeout: int = 1810):
        """保存内部地址和 token；token 只存在内存，不写入日志或结果文件。"""
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.opener = opener or urllib.request.urlopen
        self.timeout = max(1, int(timeout))

    @property
    def configured(self) -> bool:
        """判断 URL 和不可为空 token 是否同时存在。"""
        return bool(self.url and self.token)

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None, *, timeout: int | None = None) -> tuple[int, dict[str, object]]:
        """发送 JSON 请求并将 HTTP/JSON 故障映射为稳定错误。"""
        if not self.url or not self.token:
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
            if not isinstance(code, str):
                code = "agent_executor_http_error"
            if exc.code in {401, 403}:
                code = "agent_executor_unauthorized"
            raise AgentExecutorError(code, str(message or "Agent executor 请求失败")[:500]) from exc
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
        )

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

    def run(self, *, task_id: str, image_relative_path: str, reverse_image_policy: str, timeout_seconds: int, callback_token: str | None = None) -> ExecutorTaskResponse:
        """提交固定语境任务并同步等待其终态。"""
        _status, value = self._request(
            "POST",
            "/v1/tasks",
            {
                "task_id": task_id,
                "image_relative_path": image_relative_path,
                "reverse_image_policy": reverse_image_policy,
                "timeout_seconds": int(timeout_seconds),
                "wait": True,
                **({"callback_token": callback_token} if callback_token else {}),
            },
            timeout=max(self.timeout, int(timeout_seconds) + 10),
        )
        response = self._response(value)
        if response.status == "failed":
            error = response.error or {}
            code = error.get("error") or "agent_process_failed"
            raise AgentExecutorError(code, str(error.get("message") or code)[:500])
        if response.status == "cancelled":
            raise AgentExecutorError("task_interrupted", "Agent 任务已取消")
        if response.status not in {"succeeded", "running", "queued"}:
            raise AgentExecutorError("agent_executor_invalid_response", "Agent executor 任务状态无效")
        return response

    def cancel(self, task_id: str) -> ExecutorTaskResponse:
        """取消指定任务，超时或服务关闭时调用。"""
        _status, value = self._request("POST", f"/v1/tasks/{task_id}/cancel", timeout=min(10, self.timeout))
        return self._response(value)

    def status(self, task_id: str) -> ExecutorTaskResponse:
        """读取指定任务状态，用于诊断和异步调用方。"""
        _status, value = self._request("GET", f"/v1/tasks/{task_id}", timeout=min(10, self.timeout))
        return self._response(value)
