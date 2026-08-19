"""受控 OpenCode executor HTTP 服务。

该模块运行在 ``mememeow-agent-runtime`` 容器内，是后端与 OpenCode 之间的
唯一执行边界。它只接受固定字段的研究任务，不接受 shell、任意命令、任意
环境变量或任意工作目录；任务结果仍通过共享 ``/runtime/task-results`` 文件
协议交付给后端。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from backend.opencode import RUNTIME_OPENCODE_CONFIG
from backend.opencode_workspace import (
    SELECTOR_RE,
    WorkspaceCapabilityError,
    WorkspaceCapabilitySigner,
    build_edit_permission_rules,
    validate_directory_path,
    validate_file_path,
)
from executor.token import ExecutorTokenError, ensure_token_file, read_token_file

TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")
CONFIG_HASH_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
RESULT_FILE_NAME = "result.json.tmp"
RESULT_DRAFT_NAME = "result.json.draft"
ATTEMPT_METADATA_NAME = ".executor-attempts.json"
RUNTIME_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_RUNTIME_ROOT", "/runtime"))
IMAGE_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_IMAGE_ROOT", "/images"))
WORKSPACE = RUNTIME_ROOT / "workspace"
RESULT_ROOT = RUNTIME_ROOT / "task-results"
LOG_ROOT = RUNTIME_ROOT / "logs"
SKILL_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_SKILL_ROOT", "/skills/research-meme-context"))
WORKSPACE_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_WORKSPACE_ROOT", str(RUNTIME_ROOT / "workspaces")))
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
ALLOWED_REQUEST_FIELDS = frozenset(
    {
        "task_id",
        "business_task_id",
        "executor_attempt_id",
        "resume_of_attempt_id",
        "session_id",
        "processing_config_hash",
        "image_relative_path",
        "reverse_image_policy",
        "timeout_seconds",
        "wait",
        "callback_token",
        "workspace_selector",
        "workspace_capability",
    }
)
REQUIRED_RESULT_FIELDS = frozenset(
    {"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"}
)
TASK_HISTORY_LIMIT = 5000
_EXECUTOR_ERROR_CODES = frozenset(
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
    }
)
_SECRET_PATTERNS = (
    # 分开处理 JSON 字符串值和普通 header/日志格式，保留原有引号结构。
    re.compile(r"(?i)(authorization\s*[\"']?\s*:\s*[\"']?bearer\s+)[^\s,;}\]\"']+"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*:\s*)([\"'])(.*?)\2"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*[:=]\s*)[^\s,;}\]\"']+"),
)
_PATH_PATTERN = re.compile(r"(?:/runtime|/images|/skills|/app|[A-Za-z]:\\)[^\s,;]+")
_GENERIC_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s,;:'\"()\[\]{}]+/)+[^\s,;:'\"()\[\]{}]+")


class _ProcessFailure(RuntimeError):
    """保存 OpenCode 进程失败的稳定错误码和可选 HTTP 状态。"""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        """初始化进程失败；message 只允许作为有限诊断返回。"""
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取并限制 executor 的整数配置，非法值使用安全默认值。"""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _json_error(code: str, message: str) -> dict[str, object]:
    """构造不包含本地路径、命令或秘密的稳定错误响应。"""
    return {"error": code, "message": message}


def _redact_diagnostic(value: str, secrets: tuple[str, ...] = ()) -> str:
    """从上游诊断中移除已知密钥和常见凭据格式，保留有限可读错误。"""
    value = value.splitlines()[0] if value.splitlines() else value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            value = pattern.sub(r"\1\2[REDACTED]\2", value)
        else:
            value = pattern.sub(r"\1[REDACTED]", value)
    value = _PATH_PATTERN.sub("[PATH]", value)
    value = _GENERIC_PATH_PATTERN.sub("[PATH]", value)
    return value[:500]


def _safe_json(value: Any) -> bytes:
    """序列化受控响应，避免响应中出现非 JSON 值。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _session_id_from_event(value: Any) -> str | None:
    """从 OpenCode JSONL 事件递归提取 session 标识。"""
    if isinstance(value, dict):
        for key in ("session_id", "sessionID", "sessionId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            found = _session_id_from_event(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _session_id_from_event(nested)
            if found:
                return found
    return None


def _diagnostic(stdout: bytes, stderr: bytes, *, secrets: tuple[str, ...] = ()) -> str:
    """从有限输出中提取安全诊断，不返回完整 transcript 或密钥。"""
    text = stderr[:2048].decode("utf-8", errors="replace").strip()
    if text:
        return _redact_diagnostic(text, secrets)
    for line in stdout.splitlines()[:128]:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            error = event.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, str) and message.strip():
                return _redact_diagnostic(message.strip(), secrets)
    return "OpenCode 进程执行失败"


def _stream_sample(stream: Any, limit: int) -> bytes:
    """读取临时输出的头尾样本，避免大 JSONL 将 provider 错误留在不可见尾部。"""
    stream.seek(0)
    head = stream.read(limit)
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size <= limit:
        return head
    stream.seek(max(0, size - limit))
    return head + b"\n" + stream.read(limit)


def _relative_image_path(value: object) -> Path:
    """验证图片相对路径，拒绝绝对路径、父级跳转和符号链接入口。"""
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("agent_image_path_forbidden")
    raw_parts = value.split("/")
    if any(not part or part in {".", ".."} for part in raw_parts) or any(ord(character) < 0x20 for character in value):
        raise ValueError("agent_image_path_forbidden")
    normalized = PurePosixPath(value)
    if normalized.is_absolute() or not normalized.parts:
        raise ValueError("agent_image_path_forbidden")
    return Path(*normalized.parts)


@dataclass
class TaskState:
    """一个 executor attempt 的受控状态和取消句柄。"""

    task_id: str
    business_task_id: str
    executor_attempt_id: str
    image_relative_path: str
    reverse_image_policy: str
    timeout_seconds: int
    processing_config_hash: str | None = None
    session_id: str | None = None
    resume_of_attempt_id: str | None = None
    is_resume: bool = False
    callback_token: str | None = field(default=None, repr=False)
    workspace_selector: str = "local"
    workspace_capability: str | None = field(default=None, repr=False)
    workspace_directory: Path | None = field(default=None, repr=False)
    images_root: Path | None = field(default=None, repr=False)
    metadata_root: Path | None = field(default=None, repr=False)
    skill_root: Path | None = field(default=None, repr=False)
    task_scratch_root: Path | None = field(default=None, repr=False)
    config_file: Path | None = field(default=None, repr=False)
    config_dir: Path | None = field(default=None, repr=False)
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    process_reaped: bool = True
    error: dict[str, object] | None = None
    result_path: str | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, object]:
        """返回 API 可见状态，不暴露进程对象和 executor 本地实现细节。"""
        return {
            "task_id": self.business_task_id,
            "business_task_id": self.business_task_id,
            "executor_attempt_id": self.executor_attempt_id,
            "status": self.status,
            "image_relative_path": self.image_relative_path,
            "reverse_image_policy": self.reverse_image_policy,
            "session_id": self.session_id,
            "resume_of_attempt_id": self.resume_of_attempt_id,
            "processing_config_hash": self.processing_config_hash,
            "is_resume": self.is_resume,
            "result_path": self.result_path,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "process_reaped": self.process_reaped,
            "workspace_selector": self.workspace_selector,
        }


@dataclass(frozen=True)
class WorkspaceLayout:
    """executor 内部已验证的 selector 目录布局。"""

    selector: str
    directory: Path
    images_root: Path
    metadata_root: Path
    skill_root: Path
    task_scratch_root: Path
    config_file: Path
    config_dir: Path
    local: bool = False


class Executor:
    """在容器内管理固定 OpenCode 任务接口的并发执行器。"""

    def __init__(self) -> None:
        """初始化共享目录、认证配置和有限并发池。"""
        # Agent 运行身份创建的 runtime 文件不应继承镜像默认 umask 的 group/other 位。
        os.umask(0o077)
        self.token_file = os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", "").strip()
        # 环境变量兼容显式 host/测试夹具，但空白 token 必须和 token 文件一样失败关闭。
        self.token = os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "").strip()
        self.token_error: str | None = None
        if not self.token and self.token_file:
            try:
                self.token = ensure_token_file(self.token_file)
            except ExecutorTokenError as exc:
                self.token_error = str(exc)
        self.model = os.getenv("MEMEMEOW_OPENCODE_MODEL", "")
        self.base_url = os.getenv("MEMEMEOW_OPENCODE_BASE_URL", "")
        self.api_key = os.getenv("MEMEMEOW_OPENCODE_API_KEY", "")
        self.opencode_executable = os.getenv("MEMEMEOW_OPENCODE_EXECUTABLE", "opencode")
        self.max_workers = _env_int("MEMEMEOW_OPENCODE_CONCURRENCY", 1, 1, 8)
        self.backpressure = _env_int("MEMEMEOW_AGENT_BACKPRESSURE", 32, 1, 500)
        self.max_timeout = _env_int("MEMEMEOW_AGENT_EXECUTOR_MAX_TIMEOUT_SECONDS", 1800, 1, 7200)
        self.max_result_bytes = _env_int("MEMEMEOW_AGENT_RESULT_MAX_BYTES", DEFAULT_MAX_RESULT_BYTES, 1024, 16 * 1024 * 1024)
        capability_key = os.getenv("MEMEMEOW_WORKSPACE_CAPABILITY_KEY", os.getenv("MEMEMEOW_AGENT_WORKSPACE_CAPABILITY_KEY", ""))
        self.capability_signer = WorkspaceCapabilitySigner(capability_key)
        # workspace root 只允许由部署预装配；executor 不因请求 selector 创建新的
        # scope 目录。未配置时仍保留旧 local 请求兼容路径。
        self.workspace_root = Path(os.getenv("MEMEMEOW_EXECUTOR_WORKSPACE_ROOT", str(WORKSPACE_ROOT)))
        self.lock = threading.RLock()
        self.tasks: dict[str, TaskState] = {}
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="mememeow-opencode")
        self.futures: dict[str, Future[None]] = {}
        self.ready_error: str | None = None
        self._prepare_runtime()

    def _prepare_runtime(self) -> None:
        """创建共享 runtime 目录并确保 executor 以非 root 可写方式启动。"""
        try:
            for path in (RUNTIME_ROOT, WORKSPACE, RESULT_ROOT, LOG_ROOT):
                path.mkdir(parents=True, exist_ok=True)
            if any(not os.access(path, os.R_OK | os.W_OK) for path in (RUNTIME_ROOT, WORKSPACE, RESULT_ROOT, LOG_ROOT)):
                self.ready_error = "runtime_not_writable"
                return
            if self.token_error:
                self.ready_error = self.token_error
            elif not self.token:
                self.ready_error = "executor_token_not_configured"
            elif not shutil_which(self.opencode_executable):
                self.ready_error = "opencode_executable_missing"
        except OSError:
            self.ready_error = "runtime_not_ready"

    def health(self) -> dict[str, object]:
        """返回真实 executor 健康状态，供 Compose 和后端探针使用。"""
        runtime_ok = RUNTIME_ROOT.is_dir() and os.access(RUNTIME_ROOT, os.R_OK | os.W_OK)
        workspace_ok = WORKSPACE.is_dir() and os.access(WORKSPACE, os.R_OK | os.W_OK)
        result_ok = RESULT_ROOT.is_dir() and os.access(RESULT_ROOT, os.R_OK | os.W_OK)
        logs_ok = LOG_ROOT.is_dir() and os.access(LOG_ROOT, os.R_OK | os.W_OK)
        image_ok = IMAGE_ROOT.is_dir() and os.access(IMAGE_ROOT, os.R_OK) and not os.access(IMAGE_ROOT, os.W_OK)
        skill_ok = SKILL_ROOT.is_dir() and os.access(SKILL_ROOT, os.R_OK) and not os.access(SKILL_ROOT, os.W_OK)
        executable_ok = bool(shutil_which(self.opencode_executable))
        socket_absent = not Path("/var/run/docker.sock").exists()
        token_ok = bool(self.token)
        if self.token_file:
            try:
                token_ok = hmac.compare_digest(read_token_file(self.token_file), self.token)
            except ExecutorTokenError:
                token_ok = False
        ready = bool(not self.ready_error and token_ok and runtime_ok and workspace_ok and result_ok and logs_ok and image_ok and skill_ok and executable_ok and socket_absent)
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "executor": "mememeow-agent-executor",
            "opencode": executable_ok,
            "runtime_read_write": runtime_ok and workspace_ok and result_ok and logs_ok,
            "images_read_only": image_ok,
            "skills_read_only": skill_ok,
            "docker_socket_absent": socket_absent,
            "token_configured": token_ok,
            "opencode_configured": bool(self.model and self.base_url and self.api_key),
            "capacity": self.max_workers,
            "queued": self._queued_count(),
            "error": self.ready_error,
        }

    def _queued_count(self) -> int:
        """返回尚未开始的任务数量，调用方可在锁外使用。"""
        with self.lock:
            return sum(1 for task in self.tasks.values() if task.status == "queued")

    @staticmethod
    def _checked_directory(path: Path, *, create: bool = False, missing_code: str = "opencode_workspace_invalid") -> Path:
        """在 executor 边界逐级拒绝符号链接、文件和越界目录。"""
        try:
            return validate_directory_path(path, create=create, code=missing_code)
        except Exception as exc:  # noqa: BLE001 - executor 对外只返回稳定错误码
            raise ValueError(missing_code) from exc

    def _workspace_layout(self, *, selector: str | None, business_task_id: str) -> WorkspaceLayout:
        """把 selector 解析到固定布局；未知 selector 不创建任何 scope 目录。"""
        if selector is None:
            directory = self._checked_directory(WORKSPACE)
            images_root = self._checked_directory(IMAGE_ROOT)
            metadata_root = images_root
            skill_root = self._checked_directory(SKILL_ROOT)
            base = directory
            local = True
        else:
            if not isinstance(selector, str) or not SELECTOR_RE.fullmatch(selector):
                raise ValueError("opencode_workspace_invalid")
            if selector == "local":
                directory = self._checked_directory(WORKSPACE)
                images_root = self._checked_directory(IMAGE_ROOT)
                metadata_root = images_root
                skill_root = self._checked_directory(SKILL_ROOT)
                local = True
                task_scratch = directory / "tasks" / business_task_id
                return WorkspaceLayout(
                    selector="local",
                    directory=directory,
                    images_root=images_root,
                    metadata_root=metadata_root,
                    skill_root=skill_root,
                    task_scratch_root=task_scratch,
                    config_file=task_scratch / "opencode.json",
                    config_dir=task_scratch / ".opencode",
                    local=True,
                )
            root = self._checked_directory(self.workspace_root, missing_code="opencode_workspace_invalid")
            base = root / selector
            self._checked_directory(base, missing_code="opencode_workspace_invalid")
            directory = self._checked_directory(base / "workspace")
            images_root = self._checked_directory(base / "images")
            metadata_root = self._checked_directory(base / "metadata")
            skill_root = self._checked_directory(base / "skills")
            local = False
        task_scratch = directory / "tasks" / business_task_id
        # 只检查祖先；创建由 _run 在锁定任务后完成，校验失败不会留下目录。
        tasks_root = directory / "tasks"
        try:
            tasks_root.lstat()
        except FileNotFoundError:
            pass
        else:
            self._checked_directory(tasks_root)
        config_dir = task_scratch / ".opencode"
        return WorkspaceLayout(
            selector=selector or "local",
            directory=directory,
            images_root=images_root,
            metadata_root=metadata_root,
            skill_root=skill_root,
            task_scratch_root=task_scratch,
            config_file=task_scratch / "opencode.json",
            config_dir=config_dir,
            local=local,
        )

    def _verify_workspace_capability(self, values: dict[str, object]) -> None:
        """校验 selector capability 的签名、受众、期限和 attempt 绑定。"""
        selector = values.get("workspace_selector")
        capability = values.get("workspace_capability")
        if selector is None:
            if capability is not None:
                raise ValueError("opencode_workspace_mismatch")
            return
        if selector == "local" and capability is None:
            # 显式 local 仍兼容旧单用户调用；非 local selector 永远需要签名材料。
            return
        if not isinstance(capability, str) or not capability:
            raise ValueError("opencode_workspace_capability_unavailable")
        if not self.capability_signer.configured:
            raise ValueError("opencode_workspace_capability_unavailable")
        try:
            self.capability_signer.verify(
                capability,
                task_id=str(values["business_task_id"]),
                attempt_id=str(values["executor_attempt_id"]),
                selector=str(selector),
                session_id=str(values["session_id"]) if isinstance(values.get("session_id"), str) else None,
                resume_of_attempt_id=str(values["resume_of_attempt_id"]) if isinstance(values.get("resume_of_attempt_id"), str) else None,
            )
        except WorkspaceCapabilityError as exc:
            raise ValueError(exc.code) from exc

    def _verify_task_workspace_capability(self, task: TaskState) -> None:
        """在排队任务真正启动前再次验证 capability，避免长队列越过有效期。"""
        if task.workspace_selector == "local":
            return
        if not task.workspace_capability or not self.capability_signer.configured:
            raise RuntimeError("opencode_workspace_capability_unavailable")
        try:
            self.capability_signer.verify(
                task.workspace_capability,
                task_id=task.business_task_id,
                attempt_id=task.executor_attempt_id,
                selector=task.workspace_selector,
                session_id=task.session_id,
                resume_of_attempt_id=task.resume_of_attempt_id,
            )
        except WorkspaceCapabilityError as exc:
            raise RuntimeError(exc.code) from exc

    def _prune_history_locked(self) -> None:
        """限制内存中的终态历史，避免长期运行的 executor 被任务标识耗尽内存。"""
        overflow = len(self.tasks) - TASK_HISTORY_LIMIT
        if overflow <= 0:
            return
        terminal = sorted(
            (task for task in self.tasks.values() if task.status in {"succeeded", "failed", "cancelled"}),
            key=lambda task: (task.completed_at or task.created_at, task.created_at, task.task_id),
        )
        for task in terminal[:overflow]:
            self.tasks.pop(task.executor_attempt_id, None)
            self.futures.pop(task.executor_attempt_id, None)

    def _attempt_metadata_path(self, business_task_id: str) -> Path:
        """返回任务专属 attempt 元数据路径，并验证业务标识可安全拼接。"""
        if not TASK_ID_RE.fullmatch(business_task_id):
            raise ValueError("agent_result_path_invalid")
        root_info = RESULT_ROOT.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("agent_result_path_invalid")
        result_dir = RESULT_ROOT / business_task_id
        if result_dir.is_symlink() or (result_dir.exists() and not result_dir.is_dir()):
            raise ValueError("agent_result_path_invalid")
        return result_dir / ATTEMPT_METADATA_NAME

    def _metadata_signature(self, attempts: list[dict[str, object]]) -> str:
        """使用 executor token 对可恢复 attempt 事实做完整性签名。"""
        raw = json.dumps(attempts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.token.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    def _load_attempt_metadata(self, business_task_id: str) -> list[dict[str, object]]:
        """读取并验证 runtime 中持久化的 attempt 绑定，损坏时返回空集合。"""
        try:
            path = self._attempt_metadata_path(business_task_id)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 256 * 1024:
                return []
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, 256 * 1024 + 1)
            finally:
                os.close(descriptor)
            document = json.loads(raw.decode("utf-8"))
            attempts = document.get("attempts") if isinstance(document, dict) else None
            signature = document.get("signature") if isinstance(document, dict) else None
            if not isinstance(attempts, list) or not isinstance(signature, str):
                return []
            values = [item for item in attempts if isinstance(item, dict)]
            if len(values) != len(attempts) or not hmac.compare_digest(signature, self._metadata_signature(values)):
                return []
            return values[-32:]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return []

    def _persist_attempt_metadata(self, task: TaskState) -> None:
        """原子保存终态 attempt 的最小绑定事实，供 executor 重启后恢复。"""
        # 同一业务任务的两个 attempt 可能几乎同时收束；锁住读-改-写序列，
        # 避免后完成的 attempt 覆盖前一个失败事实，导致重启后无法续跑。
        with self.lock:
            self._persist_attempt_metadata_locked(task)

    def _persist_attempt_metadata_locked(self, task: TaskState) -> None:
        """在 executor 锁内原子保存 attempt 元数据，调用者负责持有锁。"""
        path = self._attempt_metadata_path(task.business_task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._load_attempt_metadata(task.business_task_id)
        entry: dict[str, object] = {
            "business_task_id": task.business_task_id,
            "executor_attempt_id": task.executor_attempt_id,
            "image_relative_path": task.image_relative_path,
            "reverse_image_policy": task.reverse_image_policy,
            "processing_config_hash": task.processing_config_hash,
            "workspace_selector": task.workspace_selector,
            "session_id": task.session_id,
            "resume_of_attempt_id": task.resume_of_attempt_id,
            "status": task.status,
            "error": task.error,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
            "process_reaped": task.process_reaped,
        }
        values = [item for item in existing if item.get("executor_attempt_id") != task.executor_attempt_id]
        values.append(entry)
        document = {"attempts": values[-32:], "signature": self._metadata_signature(values[-32:])}
        raw = _safe_json(document)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".executor-attempts.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _persisted_resume_source(self, values: dict[str, object]) -> TaskState | None:
        """从签名元数据恢复失败 attempt，供 executor 服务重启后续跑。"""
        business_task_id = str(values["business_task_id"])
        session_id = values.get("session_id")
        resume_of = values.get("resume_of_attempt_id")
        if not isinstance(session_id, str):
            return None
        for entry in reversed(self._load_attempt_metadata(business_task_id)):
            if (
                entry.get("business_task_id") != business_task_id
                or entry.get("session_id") != session_id
                or entry.get("image_relative_path") != values.get("image_relative_path")
                or entry.get("reverse_image_policy") != values.get("reverse_image_policy")
                or entry.get("processing_config_hash") != values.get("processing_config_hash")
                or entry.get("workspace_selector", "local") != (values.get("workspace_selector") or "local")
                or (resume_of is not None and entry.get("executor_attempt_id") != resume_of)
            ):
                continue
            if entry.get("status") != "failed" or entry.get("process_reaped") is not True or not isinstance(entry.get("executor_attempt_id"), str):
                continue
            error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
            if error.get("error") not in {
                "agent_provider_rate_limited",
                "agent_provider_server_error",
                "agent_connection_interrupted",
                "agent_process_failed",
            }:
                continue
            return TaskState(
                task_id=business_task_id,
                business_task_id=business_task_id,
                executor_attempt_id=str(entry["executor_attempt_id"]),
                image_relative_path=str(entry["image_relative_path"]),
                reverse_image_policy=str(entry["reverse_image_policy"]),
                timeout_seconds=int(values["timeout_seconds"]),
                processing_config_hash=values.get("processing_config_hash") if isinstance(values.get("processing_config_hash"), str) else None,
                session_id=session_id,
                workspace_selector=str(values.get("workspace_selector") or "local"),
                status="failed",
                error={str(key): value for key, value in error.items()},
                created_at=float(entry.get("created_at")) if isinstance(entry.get("created_at"), (int, float)) else time.time(),
                completed_at=float(entry.get("completed_at")) if isinstance(entry.get("completed_at"), (int, float)) else time.time(),
                process_reaped=True,
            )
        return None

    def _validate_request(self, payload: object) -> dict[str, object]:
        """校验固定任务字段并返回业务任务与 attempt 的规范化参数。"""
        if not isinstance(payload, dict):
            raise ValueError("invalid_task")
        unknown = set(payload) - ALLOWED_REQUEST_FIELDS
        if unknown:
            raise ValueError("invalid_task")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("agent_result_path_invalid")
        business_task_id = payload.get("business_task_id", task_id)
        if not isinstance(business_task_id, str) or not TASK_ID_RE.fullmatch(business_task_id) or business_task_id != task_id:
            raise ValueError("invalid_task")
        raw_attempt_id = payload.get("executor_attempt_id")
        # 旧客户端没有 attempt 字段时使用业务 ID 作为兼容别名；新客户端总是
        # 发送独立 attempt，因而终态历史不会被恢复请求复用。
        executor_attempt_id = raw_attempt_id if raw_attempt_id is not None else business_task_id
        if not isinstance(executor_attempt_id, str) or not TASK_ID_RE.fullmatch(executor_attempt_id):
            raise ValueError("invalid_task")
        raw_resume_of = payload.get("resume_of_attempt_id")
        if raw_resume_of is not None and (
            not isinstance(raw_resume_of, str) or not TASK_ID_RE.fullmatch(raw_resume_of)
        ):
            raise ValueError("invalid_task")
        session_id = payload.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id)):
            raise ValueError("session_binding_mismatch")
        if raw_resume_of is not None and session_id is None:
            raise ValueError("session_binding_mismatch")
        processing_config_hash = payload.get("processing_config_hash")
        if processing_config_hash is not None and (
            not isinstance(processing_config_hash, str) or not CONFIG_HASH_RE.fullmatch(processing_config_hash)
        ):
            raise ValueError("invalid_task")
        relative = _relative_image_path(payload.get("image_relative_path"))
        raw_selector = payload.get("workspace_selector")
        if raw_selector is None or raw_selector == "local":
            image = IMAGE_ROOT / relative
            try:
                current = IMAGE_ROOT
                for part in relative.parts:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError("agent_image_path_forbidden")
                root = IMAGE_ROOT.resolve()
                candidate = image.resolve(strict=True)
                candidate.relative_to(root)
                if not candidate.is_file():
                    raise ValueError("agent_image_path_forbidden")
            except (OSError, RuntimeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc) == "agent_image_path_forbidden":
                    raise
                raise ValueError("agent_image_path_forbidden") from exc
        policy = payload.get("reverse_image_policy", "forbid")
        if policy not in {"forbid", "auto"}:
            raise ValueError("invalid_reverse_image_policy")
        raw_timeout = payload.get("timeout_seconds", self.max_timeout)
        if isinstance(raw_timeout, bool):
            raise ValueError("invalid_task")
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_task") from exc
        if timeout < 1 or timeout > self.max_timeout:
            raise ValueError("agent_timeout_limit_exceeded")
        wait = payload.get("wait", True)
        if not isinstance(wait, bool):
            raise ValueError("invalid_task")
        callback_token = payload.get("callback_token")
        if callback_token is not None and (
            not isinstance(callback_token, str)
            or len(callback_token) > 4096
            or any(character.isspace() or ord(character) < 0x20 for character in callback_token)
        ):
            raise ValueError("invalid_task")
        workspace_selector = raw_selector
        if workspace_selector is not None and (not isinstance(workspace_selector, str) or not SELECTOR_RE.fullmatch(workspace_selector)):
            raise ValueError("opencode_workspace_invalid")
        workspace_capability = payload.get("workspace_capability")
        if workspace_capability is not None and (not isinstance(workspace_capability, str) or not workspace_capability or len(workspace_capability) > 4096):
            raise ValueError("opencode_workspace_capability_invalid")
        values = {
            "task_id": business_task_id,
            "business_task_id": business_task_id,
            "executor_attempt_id": executor_attempt_id,
            "resume_of_attempt_id": raw_resume_of,
            "session_id": session_id,
            "processing_config_hash": processing_config_hash.lower() if isinstance(processing_config_hash, str) else None,
            "image_relative_path": relative.as_posix(),
            "reverse_image_policy": str(policy),
            "timeout_seconds": timeout,
            "wait": wait,
            "callback_token": callback_token,
            "workspace_selector": workspace_selector,
            "workspace_capability": workspace_capability,
        }
        self._verify_workspace_capability(values)
        layout = self._workspace_layout(selector=workspace_selector if isinstance(workspace_selector, str) else None, business_task_id=business_task_id)
        image = layout.images_root / relative
        try:
            current = layout.images_root
            for part in relative.parts:
                current = current / part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("agent_image_path_forbidden")
            info = image.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("agent_image_path_forbidden")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) == "agent_image_path_forbidden":
                raise
            raise ValueError("agent_image_path_forbidden") from exc
        values["workspace_layout"] = layout
        return values

    def _resume_source(self, values: dict[str, object]) -> TaskState | None:
        """查找与续跑 session 完全绑定的失败 attempt，拒绝跨任务猜测。"""
        session_id = values.get("session_id")
        if not isinstance(session_id, str):
            return None
        business_task_id = values["business_task_id"]
        resume_of = values.get("resume_of_attempt_id")
        matches = [
            task
            for task in self.tasks.values()
            if task.session_id == session_id
            and task.business_task_id == business_task_id
            and task.image_relative_path == values["image_relative_path"]
            and task.reverse_image_policy == values["reverse_image_policy"]
            and task.processing_config_hash == values.get("processing_config_hash")
            and task.workspace_selector == (values.get("workspace_selector") or "local")
            and (resume_of is None or task.executor_attempt_id == resume_of)
        ]
        if not matches:
            persisted = self._persisted_resume_source(values)
            if persisted is not None:
                return persisted
            # 同一 session 若属于其它业务任务，明确返回绑定错误；完全未知的
            # session 则返回不可续跑，避免把最近会话当作恢复目标。
            if any(task.session_id == session_id for task in self.tasks.values()):
                raise RuntimeError("session_binding_mismatch")
            raise RuntimeError("session_not_resumable")
        source = max(matches, key=lambda task: (task.completed_at or task.created_at, task.created_at))
        if source.status != "failed":
            raise RuntimeError("session_not_resumable")
        if source.process_reaped is not True:
            raise RuntimeError("session_not_resumable")
        source_error = (source.error or {}).get("error")
        if source_error not in {
            "agent_provider_rate_limited",
            "agent_provider_server_error",
            "agent_connection_interrupted",
            "agent_process_failed",
        }:
            raise RuntimeError("session_not_resumable")
        return source

    def submit(self, payload: object) -> tuple[TaskState, bool]:
        """创建固定研究任务并交给受限线程池，返回状态和同步等待标记。"""
        values = self._validate_request(payload)
        with self.lock:
            self._prune_history_locked()
            if not self.health().get("ready"):
                raise RuntimeError("agent_runtime_unavailable")
            if not self.model or not self.base_url or not self.api_key:
                raise RuntimeError("opencode_not_configured")
            existing = self.tasks.get(values["executor_attempt_id"])
            if existing is not None:
                raise RuntimeError("task_exists")
            same_business = [
                task
                for task in self.tasks.values()
                if task.business_task_id == values["business_task_id"]
                and task.executor_attempt_id != values["executor_attempt_id"]
                and (task.status in {"queued", "running"} or task.process_reaped is not True)
            ]
            if same_business:
                # 同一业务 Task 只能有一个活动 attempt；未确认回收的旧进程更严格地
                # 收束为 unknown_execution，避免其晚到结果污染新 attempt。
                if not any(task.status in {"queued", "running"} for task in same_business) and any(task.process_reaped is not True for task in same_business):
                    raise RuntimeError("unknown_execution")
                raise RuntimeError("task_exists")
            if self._queued_count() >= self.backpressure:
                raise RuntimeError("agent_backpressure")
            source = self._resume_source(values) if values.get("session_id") else None
            task = TaskState(
                task_id=str(values["business_task_id"]),
                business_task_id=str(values["business_task_id"]),
                executor_attempt_id=str(values["executor_attempt_id"]),
                image_relative_path=str(values["image_relative_path"]),
                reverse_image_policy=str(values["reverse_image_policy"]),
                timeout_seconds=int(values["timeout_seconds"]),
                processing_config_hash=values.get("processing_config_hash") if isinstance(values.get("processing_config_hash"), str) else None,
                session_id=values.get("session_id") if isinstance(values.get("session_id"), str) else None,
                workspace_selector=str(values.get("workspace_selector") or "local"),
                workspace_capability=values.get("workspace_capability") if isinstance(values.get("workspace_capability"), str) else None,
                workspace_directory=(values.get("workspace_layout").directory if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                images_root=(values.get("workspace_layout").images_root if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                metadata_root=(values.get("workspace_layout").metadata_root if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                skill_root=(values.get("workspace_layout").skill_root if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                task_scratch_root=(values.get("workspace_layout").task_scratch_root if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                config_file=(values.get("workspace_layout").config_file if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                config_dir=(values.get("workspace_layout").config_dir if isinstance(values.get("workspace_layout"), WorkspaceLayout) else None),
                resume_of_attempt_id=source.executor_attempt_id if source else None,
                is_resume=source is not None,
                callback_token=values.get("callback_token") if isinstance(values.get("callback_token"), str) else None,
                result_path=f"task-results/{values['business_task_id']}/{RESULT_FILE_NAME}",
            )
            self.tasks[task.executor_attempt_id] = task
            self.futures[task.executor_attempt_id] = self.pool.submit(self._run, task)
            return task, bool(values["wait"])

    @staticmethod
    def _workspace_permission_rules(task: TaskState) -> dict[str, str]:
        """生成 OpenCode 外部目录的最后匹配优先规则。"""
        images = task.images_root or IMAGE_ROOT
        metadata = task.metadata_root or IMAGE_ROOT
        skills = task.skill_root or SKILL_ROOT
        scratch = task.task_scratch_root or (WORKSPACE / "tasks" / task.business_task_id)
        result_dir = RESULT_ROOT / task.business_task_id
        subtree = lambda path: f"{Path(os.path.abspath(path)).as_posix()}/**"
        return {
            "*": "deny",
            subtree(skills): "allow",
            subtree(images): "allow",
            subtree(metadata): "allow",
            subtree(scratch): "allow",
            # 外部文件工具先检查结果目录的 ``<dir>/*`` 父级模式；写入范围
            # 由同一配置中的 ``edit`` 规则收窄到两个固定结果文件。
            str(result_dir.absolute()) + "/*": "allow",
            str((result_dir / RESULT_DRAFT_NAME).absolute()): "allow",
            str((result_dir / RESULT_FILE_NAME).absolute()): "allow",
        }

    @staticmethod
    def _workspace_edit_permission_rules(task: TaskState) -> dict[str, str]:
        """生成只允许 Task 临时数据和两个结果文件的编辑规则。"""
        scratch = task.task_scratch_root or (WORKSPACE / "tasks" / task.business_task_id)
        config_file = task.config_file or scratch / "opencode.json"
        config_dir = task.config_dir or scratch / ".opencode"
        result_dir = RESULT_ROOT / task.business_task_id
        return dict(
            build_edit_permission_rules(
                task_scratch_root=scratch,
                config_file=config_file,
                config_dir=config_dir,
                draft_path=result_dir / RESULT_DRAFT_NAME,
                result_path=result_dir / RESULT_FILE_NAME,
            )
        )

    def _prepare_task_workspace(self, task: TaskState) -> None:
        """创建当前 Task 临时目录并原子写入不含密钥的 workspace 配置。"""
        scratch = task.task_scratch_root or (WORKSPACE / "tasks" / task.business_task_id)
        config_file = task.config_file or scratch / "opencode.json"
        config_dir = task.config_dir or scratch / ".opencode"
        self._checked_directory(scratch.parent, create=True)
        self._checked_directory(scratch, create=True)
        self._checked_directory(config_dir, create=True)
        try:
            validate_file_path(config_file, allow_missing=True, code="opencode_workspace_invalid")
        except Exception as exc:  # noqa: BLE001 - 配置路径是 executor 安全边界
            raise RuntimeError("opencode_workspace_invalid") from exc
        config = json.loads(json.dumps(RUNTIME_OPENCODE_CONFIG, ensure_ascii=False))
        config["permission"] = {
            "external_directory": self._workspace_permission_rules(task),
            "edit": self._workspace_edit_permission_rules(task),
        }
        content = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        temporary = config_file.with_name(f".{config_file.name}.tmp.{os.getpid()}.{id(task)}")
        try:
            validate_file_path(temporary, allow_missing=True, code="opencode_workspace_invalid")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), stat.S_IRUSR | stat.S_IWUSR)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    descriptor = -1
                    handle.write(content)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise RuntimeError("opencode_workspace_invalid") from exc
        try:
            os.replace(temporary, config_file)
        except OSError as exc:
            raise RuntimeError("opencode_workspace_invalid") from exc

    def _task_environment(self, task: TaskState) -> dict[str, str]:
        """构造 OpenCode 最小环境白名单，不继承 executor 容器中的无关变量。"""
        values = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.getenv("HOME", "/runtime/home"),
            "OPENCODE_DB": str(RUNTIME_ROOT / "opencode.db"),
            "OPENCODE_CONFIG": str(task.config_file or (task.task_scratch_root or (WORKSPACE / "tasks" / task.business_task_id)) / "opencode.json"),
            "OPENCODE_CONFIG_DIR": str(task.config_dir or (task.task_scratch_root or (WORKSPACE / "tasks" / task.business_task_id)) / ".opencode"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "MEMEMEOW_OPENCODE_BASE_URL": self.base_url,
            "MEMEMEOW_OPENCODE_API_KEY": self.api_key,
            "MEMEMEOW_OPENCODE_SLOT": "0",
            "MEMEMEOW_AGENT_TASK_ID": task.business_task_id,
            "MEMEMEOW_AGENT_EXECUTOR_ATTEMPT_ID": task.executor_attempt_id,
            **({"MEMEMEOW_AGENT_CALLBACK_TOKEN": task.callback_token} if task.callback_token else {}),
            "MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL": os.getenv("MEMEMEOW_AGENT_REVERSE_IMAGE_INTERNAL_URL", ""),
            "MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL": os.getenv("MEMEMEOW_AGENT_VISUAL_SEARCH_INTERNAL_URL", ""),
            "MEMEMEOW_DATA_ROOT": str(RUNTIME_ROOT),
            "MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT": str(RUNTIME_ROOT / "reverse_image_cache" / "serpapi_google_lens"),
            # 依赖位于镜像只读目录，避免在 runtime volume 内创建 node_modules 链接。
            "NODE_PATH": "/opt/mememeow/node_modules",
        }
        return values

    def _prompt(self, task: TaskState) -> str:
        """生成唯一业务 prompt；调用方不能覆盖路径、命令或提示词。"""
        result_dir = RESULT_ROOT / task.task_id
        resume_instruction = (
            "这是同一 OpenCode session 的受控续跑；保留并检查已有草稿和中间产物，只完成尚未完成的工作。"
            if task.is_resume
            else "这是首次执行；先建立任务草稿并逐步完成研究。"
        )
        return (
            "使用 research-meme-context skill 分析这张表情包；只通过项目内部接口使用可选反向图片能力。"
            f"本任务 reverse_image_policy={task.reverse_image_policy}。"
            f"{resume_instruction}"
            f"结果必须写入 {result_dir / RESULT_FILE_NAME}；先写 {result_dir / RESULT_DRAFT_NAME}，"
            "使用 output-schema.json 校验后通过同一文件系统的原子 rename/mv 替换最终文件。"
            "不要把业务 JSON 作为 assistant 文本交付，不要写入数据库。"
        )

    def _run(self, task: TaskState) -> None:
        """执行单个固定 OpenCode 任务并更新状态，失败时只保存稳定诊断。"""
        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        stderr = b""
        timed_out = False
        try:
            with self.lock:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    task.error = _json_error("task_interrupted", "任务已取消")
                    task.completed_at = time.time()
                    task.done.set()
                    return
                self._verify_task_workspace_capability(task)
                task.status = "running"
                task.started_at = time.time()
                self._prepare_task_workspace(task)
                result_dir = RESULT_ROOT / task.task_id
                if result_dir.is_symlink() or (result_dir.exists() and not result_dir.is_dir()):
                    raise RuntimeError("agent_result_path_invalid")
                result_dir.mkdir(parents=True, exist_ok=True)
                # 续跑必须保留原 attempt 的草稿和已验证结果，首次 attempt 才清理
                # 任务目录中的两个受控交付文件。
                if not task.is_resume:
                    for filename in (RESULT_FILE_NAME, RESULT_DRAFT_NAME):
                        path = result_dir / filename
                        if path.exists() or path.is_symlink():
                            path.unlink()
            env = self._task_environment(task)
            image_root = task.images_root or IMAGE_ROOT
            image = image_root / _relative_image_path(task.image_relative_path)
            try:
                self._checked_directory(image_root)
                current = image_root
                for part in _relative_image_path(task.image_relative_path).parts:
                    current = current / part
                    info = current.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        raise ValueError("agent_image_path_forbidden")
                info = image.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("agent_image_path_forbidden")
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc) == "agent_image_path_forbidden":
                    raise
                raise ValueError("agent_image_path_forbidden") from exc
            command = [
                self.opencode_executable,
                "run",
                "--auto",
                "--dir",
                str(task.workspace_directory or WORKSPACE),
                "--format",
                "json",
                "--file",
                str(image),
                "--model",
                self.model,
                "--variant",
                "max",
                "--title",
                f"mememeow-task-{task.task_id}",
            ]
            if task.session_id:
                command.extend(("--session", task.session_id))
            command.append(self._prompt(task))
            with tempfile.TemporaryFile(dir=LOG_ROOT, prefix=f"{task.task_id}-", mode="w+b") as out, tempfile.TemporaryFile(dir=LOG_ROOT, prefix=f"{task.task_id}-", mode="w+b") as err:
                process = subprocess.Popen(command, cwd=task.workspace_directory or WORKSPACE, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
                with self.lock:
                    task.process = process
                    # 进程启动后，只有父进程确认 waitpid 收束才允许该 attempt 作为续跑源。
                    task.process_reaped = False
                deadline = time.monotonic() + task.timeout_seconds
                while process.poll() is None:
                    if task.cancel_event.is_set():
                        reaped = self._terminate(process)
                        with self.lock:
                            task.process_reaped = reaped
                        if not reaped:
                            raise _ProcessFailure("unknown_execution", "无法确认 OpenCode 进程已终止")
                        break
                    if time.monotonic() >= deadline:
                        reaped = self._terminate(process)
                        with self.lock:
                            task.process_reaped = reaped
                        if not reaped:
                            raise _ProcessFailure("unknown_execution", "无法确认 OpenCode 进程已终止")
                        timed_out = True
                        break
                    time.sleep(0.05)
                if process.poll() is not None:
                    with self.lock:
                        task.process_reaped = True
                out.flush()
                err.flush()
                out.seek(0)
                err.seek(0)
                stdout = _stream_sample(out, 256 * 1024)
                stderr = _stream_sample(err, 16 * 1024)
            self._capture_session(task, stdout)
            if task.cancel_event.is_set():
                raise RuntimeError("task_interrupted")
            if timed_out:
                raise _ProcessFailure("agent_timeout", "OpenCode 执行超时")
            if process.returncode != 0:
                code, http_status = self._classify_process_failure(stdout, stderr)
                raise _ProcessFailure(
                    code,
                    _diagnostic(stdout, stderr, secrets=(self.api_key, self.token, task.callback_token or "")),
                    http_status=http_status,
                )
            if not task.session_id:
                raise RuntimeError("agent_output_invalid_json")
            self._validate_result_file(RESULT_ROOT / task.task_id / RESULT_FILE_NAME)
            with self.lock:
                if task.cancel_event.is_set():
                    raise RuntimeError("task_interrupted")
                task.status = "succeeded"
                task.completed_at = time.time()
        except _ProcessFailure as exc:
            with self.lock:
                # 未确认进程已回收时，取消/超时也不能伪装成可安全重试的普通失败。
                code = exc.code
                if task.cancel_event.is_set() and code != "unknown_execution":
                    code = "task_interrupted"
                task.status = "cancelled" if code == "task_interrupted" else "failed"
                error: dict[str, object] = {
                    "error": code,
                    "message": _redact_diagnostic(exc.args[0], (self.api_key, self.token, task.callback_token or "")),
                }
                if exc.http_status is not None:
                    error["http_status"] = exc.http_status
                task.error = error
                task.completed_at = time.time()
        except RuntimeError as exc:
            code, _, detail = str(exc).partition(":")
            with self.lock:
                # 取消与子进程自然退出可能同时发生；持锁后以取消为最终事实，避免把
                # 用户明确取消的任务误记为普通进程失败。
                if process is not None and process.poll() is not None:
                    task.process_reaped = True
                if task.process_reaped is not True:
                    code, detail = "unknown_execution", "无法确认 OpenCode 进程已终止"
                elif task.cancel_event.is_set():
                    code, detail = "task_interrupted", ""
                if code not in _EXECUTOR_ERROR_CODES:
                    code = "agent_process_failed"
                task.status = "cancelled" if code == "task_interrupted" else "failed"
                fallback = {
                    "agent_timeout": "OpenCode 执行超时",
                    "task_interrupted": "任务已取消",
                }.get(code, "任务执行失败")
                task.error = _json_error(code, _redact_diagnostic(detail, (self.api_key, self.token, task.callback_token or "")) if detail else fallback)
                task.completed_at = time.time()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            with self.lock:
                if process is not None and process.poll() is not None:
                    task.process_reaped = True
                code = "unknown_execution" if task.process_reaped is not True else "task_interrupted" if task.cancel_event.is_set() else "agent_process_failed"
                task.status = "cancelled" if code == "task_interrupted" else "failed"
                task.error = _json_error(code, "任务已取消" if code == "task_interrupted" else "无法确认 OpenCode 进程已终止" if code == "unknown_execution" else _redact_diagnostic(str(exc), (self.api_key, self.token, task.callback_token or "")))
                task.completed_at = time.time()
        except Exception as exc:  # noqa: BLE001 - 任务必须以终态收束，不能留下永久 running
            with self.lock:
                if process is not None and process.poll() is not None:
                    task.process_reaped = True
                code = "unknown_execution" if task.process_reaped is not True else "task_interrupted" if task.cancel_event.is_set() else "agent_process_failed"
                task.status = "cancelled" if code == "task_interrupted" else "failed"
                task.error = _json_error(code, "任务已取消" if code == "task_interrupted" else "无法确认 OpenCode 进程已终止" if code == "unknown_execution" else _redact_diagnostic(str(exc), (self.api_key, self.token, task.callback_token or "")))
                task.completed_at = time.time()
        finally:
            with self.lock:
                task.process = None
                try:
                    self._persist_attempt_metadata(task)
                except (OSError, ValueError, TypeError):
                    # 元数据仅用于跨重启诊断；写入失败不能让当前任务悬挂，
                    # 但此时新 executor 必须因缺少签名事实而拒绝自动续跑。
                    pass
                task.done.set()

    def _capture_session(self, task: TaskState, stdout: bytes) -> None:
        """从成功、失败和超时的有限 JSONL 输出中绑定 session 标识。"""
        found_session: str | None = None
        for line in stdout.splitlines():
            try:
                candidate = _session_id_from_event(json.loads(line))
            except (json.JSONDecodeError, RecursionError):
                continue
            if candidate and SESSION_ID_RE.fullmatch(candidate):
                if task.session_id and candidate != task.session_id:
                    raise RuntimeError("session_binding_mismatch")
                if found_session and candidate != found_session:
                    raise RuntimeError("session_binding_mismatch")
                found_session = candidate
        if found_session:
            with self.lock:
                task.session_id = found_session

    @staticmethod
    def _classify_process_failure(stdout: bytes, stderr: bytes) -> tuple[str, int | None]:
        """按有限输出区分 provider 网关、网络和普通进程暂态错误。"""
        text = (stdout[:256 * 1024] + stderr[:16 * 1024]).decode("utf-8", errors="replace").lower()
        statuses = [int(match) for match in re.findall(r"(?:statuscode|status|http)[^0-9]{0,8}(\d{3})", text)]
        if any(status == 429 for status in statuses):
            return "agent_provider_rate_limited", 429
        server_status = next((status for status in statuses if 500 <= status <= 599), None)
        if server_status is not None:
            return "agent_provider_server_error", server_status
        if any(token in text for token in ("econnreset", "connection reset", "connection refused", "socket hang up", "network error", "timed out")):
            return "agent_connection_interrupted", None
        return "agent_process_failed", None

    def _validate_result_file(self, path: Path) -> None:
        """验证结果文件位置、大小和基本 JSON 结构；完整 schema 由后端复核。"""
        current = RESULT_ROOT
        try:
            root_metadata = RESULT_ROOT.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                raise RuntimeError("agent_result_path_invalid")
            relative = path.relative_to(RESULT_ROOT)
        except ValueError as exc:
            raise RuntimeError("agent_result_path_invalid") from exc
        except OSError as exc:
            raise RuntimeError("agent_result_path_invalid") from exc
        if len(relative.parts) != 2 or relative.name != RESULT_FILE_NAME:
            raise RuntimeError("agent_result_path_invalid")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError("agent_result_path_invalid")
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError("agent_result_file_missing") from exc
        except OSError as exc:
            raise RuntimeError("agent_result_file_unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("agent_result_path_invalid")
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("agent_result_file_unreadable")
        if metadata.st_size > self.max_result_bytes:
            raise RuntimeError("agent_result_file_too_large")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                chunks: list[bytes] = []
                total = 0
                while total <= self.max_result_bytes:
                    chunk = os.read(descriptor, min(64 * 1024, self.max_result_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > self.max_result_bytes:
                    raise RuntimeError("agent_result_file_too_large")
            finally:
                os.close(descriptor)
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError("agent_result_file_missing") from exc
        except RuntimeError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeError("agent_result_file_invalid_json") from exc
        if not isinstance(value, dict) or not REQUIRED_RESULT_FIELDS.issubset(value):
            raise RuntimeError("agent_result_file_schema_invalid")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> bool:
        """终止任务进程组并返回父进程是否已被 waitpid 确认回收。"""
        if process.poll() is not None:
            return True
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except OSError:
                return process.poll() is not None
            except subprocess.TimeoutExpired:
                # 进程组仍未收束时不阻塞 executor；调用方会把任务标记为失败。
                return process.poll() is not None
        return process.poll() is not None

    def cancel(self, task_id: str) -> TaskState:
        """取消指定任务；只终止该任务进程，不影响 executor 或其他任务。"""
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status in {"succeeded", "failed", "cancelled"}:
                return task
            task.cancel_event.set()
            was_queued = task.status == "queued"
            process = task.process
            task.status = "cancelled"
            task.error = _json_error("task_interrupted", "任务已取消")
            task.completed_at = time.time()
            if process is None and was_queued:
                task.done.set()
        if process is not None:
            reaped = self._terminate(process)
            with self.lock:
                task.process_reaped = reaped
                if not reaped:
                    task.status = "failed"
                    task.error = _json_error("unknown_execution", "无法确认 OpenCode 进程已终止")
                    task.completed_at = time.time()
        return task

    def close(self) -> None:
        """停止线程池并取消仍未结束的任务。"""
        with self.lock:
            ids = [task.executor_attempt_id for task in self.tasks.values() if task.status not in {"succeeded", "failed", "cancelled"}]
        for task_id in ids:
            self.cancel(task_id)
        self.pool.shutdown(wait=False, cancel_futures=True)


def shutil_which(executable: str) -> str | None:
    """在不导入 shell 的情况下检查固定 OpenCode 可执行文件。"""
    if not executable or "/" in executable:
        path = Path(executable)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    for directory in os.getenv("PATH", "").split(os.pathsep):
        candidate = Path(directory) / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class Handler(BaseHTTPRequestHandler):
    """executor HTTP 路由，只公开健康、任务提交、状态和取消接口。"""

    server: "ExecutorHTTPServer"

    def setup(self) -> None:
        """为请求体读取设置有限超时，避免慢速连接长期占用线程。"""
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, _format: str, *_args: object) -> None:
        """禁止将认证头、任务输入或 OpenCode 诊断写入访问日志。"""
        return

    def _authorized(self) -> bool:
        """使用固定 Bearer token 验证内部 API 调用。"""
        expected = self.server.executor.token
        value = self.headers.get("Authorization", "")
        supplied = value[7:].strip() if value.startswith("Bearer ") else ""
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def _send(self, status: int, payload: object) -> None:
        """发送统一 JSON 响应并关闭连接。"""
        body = _safe_json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> object:
        """读取有限大小的 JSON 请求体，拒绝 multipart、流式 shell 等其他协议。"""
        if self.headers.get_content_type() != "application/json":
            raise ValueError("invalid_task")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise ValueError("invalid_task") from exc
        if length < 0 or length > 64 * 1024:
            raise ValueError("invalid_task")
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid_task") from exc

    def do_GET(self) -> None:  # noqa: N802
        """处理健康和任务状态查询。"""
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            if not self._authorized():
                self._send(401, _json_error("executor_unauthorized", "executor 认证失败"))
                return
            self._send(200, self.server.executor.health())
            return
        if not self._authorized():
            self._send(401, _json_error("executor_unauthorized", "executor 认证失败"))
            return
        match = re.fullmatch(r"/v1/tasks/([^/]+)", path)
        if match:
            task_id = unquote(match.group(1))
            with self.server.executor.lock:
                task = self.server.executor.tasks.get(task_id)
            if task is None:
                self._send(404, _json_error("task_not_found", "任务不存在"))
            else:
                self._send(200, task.public())
            return
        self._send(404, _json_error("not_found", "接口不存在"))

    def do_POST(self) -> None:  # noqa: N802
        """处理固定研究任务提交和任务取消。"""
        if not self._authorized():
            self._send(401, _json_error("executor_unauthorized", "executor 认证失败"))
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        cancel_match = re.fullmatch(r"/v1/tasks/([^/]+)/cancel", path)
        if cancel_match:
            try:
                task = self.server.executor.cancel(unquote(cancel_match.group(1)))
            except KeyError:
                self._send(404, _json_error("task_not_found", "任务不存在"))
                return
            self._send(200, task.public())
            return
        if path != "/v1/tasks":
            self._send(404, _json_error("not_found", "接口不存在"))
            return
        try:
            payload = self._read_json()
            task, wait = self.server.executor.submit(payload)
        except ValueError as exc:
            code = str(exc)
            messages = {
                "agent_image_path_forbidden": "图片路径不在受控图片目录内",
                "agent_result_path_invalid": "任务标识非法",
                "invalid_reverse_image_policy": "反向图片策略无效",
                "agent_timeout_limit_exceeded": "任务超时超过 executor 上限",
                "session_binding_mismatch": "续跑 session 与任务事实不匹配",
                "opencode_workspace_invalid": "workspace selector 或目录无效",
                "opencode_workspace_mismatch": "workspace capability 与任务事实不匹配",
                "opencode_workspace_capability_invalid": "workspace capability 无效",
                "opencode_workspace_capability_expired": "workspace capability 已过期",
                "opencode_workspace_capability_unavailable": "workspace capability 未配置",
            }
            if code not in messages and code not in {"invalid_task"}:
                code = "invalid_task"
            self._send(400, _json_error(code, messages.get(code, "任务请求无效")))
            return
        except RuntimeError as exc:
            code = str(exc)
            if code not in {
                "agent_backpressure",
                "task_exists",
                "agent_runtime_unavailable",
                "opencode_not_configured",
                "session_binding_mismatch",
                "session_not_resumable",
                "unknown_execution",
                "opencode_workspace_invalid",
                "opencode_workspace_mismatch",
                "opencode_workspace_capability_invalid",
                "opencode_workspace_capability_expired",
                "opencode_workspace_capability_unavailable",
            }:
                code = "agent_runtime_unavailable"
            status = {
                "agent_backpressure": 429,
                "task_exists": 409,
                "session_binding_mismatch": 409,
                "session_not_resumable": 409,
                "unknown_execution": 409,
                "agent_runtime_unavailable": 503,
                "opencode_not_configured": 503,
            }.get(code, 503)
            self._send(status, _json_error(code, "任务当前无法执行"))
            return
        if wait:
            task.done.wait(task.timeout_seconds + 5)
        status = 200 if task.status in {"succeeded", "failed", "cancelled"} else 202
        self._send(status, task.public())


class ExecutorHTTPServer(ThreadingHTTPServer):
    """带 executor 状态对象的线程化 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], executor: Executor):
        """绑定固定 loopback 监听地址供 Compose 内网转发。"""
        self.executor = executor
        super().__init__(address, Handler)


def main() -> None:
    """启动 executor；端口只由容器内部环境配置，不发布到宿主机。"""
    host = os.getenv("MEMEMEOW_AGENT_EXECUTOR_HOST", "0.0.0.0")
    port = _env_int("MEMEMEOW_AGENT_EXECUTOR_PORT", 8277, 1, 65535)
    executor = Executor()
    server = ExecutorHTTPServer((host, port), executor)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        executor.close()
        server.server_close()


if __name__ == "__main__":
    main()
