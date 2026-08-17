"""受控 OpenCode executor HTTP 服务。

该模块运行在 ``mememeow-agent-runtime`` 容器内，是后端与 OpenCode 之间的
唯一执行边界。它只接受固定字段的研究任务，不接受 shell、任意命令、任意
环境变量或任意工作目录；任务结果仍通过共享 ``/runtime/task-results`` 文件
协议交付给后端。
"""

from __future__ import annotations

import hmac
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from executor.token import ExecutorTokenError, ensure_token_file


TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
RESULT_FILE_NAME = "result.json.tmp"
RESULT_DRAFT_NAME = "result.json.draft"
RUNTIME_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_RUNTIME_ROOT", "/runtime"))
IMAGE_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_IMAGE_ROOT", "/images"))
WORKSPACE = RUNTIME_ROOT / "workspace"
RESULT_ROOT = RUNTIME_ROOT / "task-results"
LOG_ROOT = RUNTIME_ROOT / "logs"
SKILL_ROOT = Path(os.getenv("MEMEMEOW_EXECUTOR_SKILL_ROOT", "/skills/research-meme-context"))
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
ALLOWED_REQUEST_FIELDS = frozenset(
    {"task_id", "image_relative_path", "reverse_image_policy", "timeout_seconds", "wait", "callback_token"}
)
REQUIRED_RESULT_FIELDS = frozenset(
    {"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"}
)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """读取并限制 executor 的整数配置，非法值使用安全默认值。"""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _json_error(code: str, message: str) -> dict[str, str]:
    """构造不包含本地路径、命令或秘密的稳定错误响应。"""
    return {"error": code, "message": message}


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


def _diagnostic(stdout: bytes, stderr: bytes) -> str:
    """从有限输出中提取安全诊断，不返回完整 transcript 或密钥。"""
    text = stderr[:2048].decode("utf-8", errors="replace").strip()
    if text:
        return text[:500]
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
                return message.strip()[:500]
    return "OpenCode 进程执行失败"


def _relative_image_path(value: object) -> Path:
    """验证图片相对路径，拒绝绝对路径、父级跳转和符号链接入口。"""
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ValueError("agent_image_path_forbidden")
    normalized = PurePosixPath(value)
    if normalized.is_absolute() or not normalized.parts or any(part in {"", ".", ".."} for part in normalized.parts):
        raise ValueError("agent_image_path_forbidden")
    return Path(*normalized.parts)


@dataclass
class TaskState:
    """一个 executor 任务的受控状态和取消句柄。"""

    task_id: str
    image_relative_path: str
    reverse_image_policy: str
    timeout_seconds: int
    callback_token: str | None = field(default=None, repr=False)
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    session_id: str | None = None
    error: dict[str, str] | None = None
    result_path: str | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, object]:
        """返回 API 可见状态，不暴露进程对象和 executor 本地实现细节。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "image_relative_path": self.image_relative_path,
            "reverse_image_policy": self.reverse_image_policy,
            "session_id": self.session_id,
            "result_path": self.result_path,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class Executor:
    """在容器内管理固定 OpenCode 任务接口的并发执行器。"""

    def __init__(self) -> None:
        """初始化共享目录、认证配置和有限并发池。"""
        # Agent 运行身份创建的 runtime 文件不应继承镜像默认 umask 的 group/other 位。
        os.umask(0o077)
        self.token_file = os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", "").strip()
        self.token = os.getenv("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "")
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
            if not os.access(RUNTIME_ROOT, os.W_OK) or not os.access(RESULT_ROOT, os.W_OK):
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
        result_ok = RESULT_ROOT.is_dir() and os.access(RESULT_ROOT, os.R_OK | os.W_OK)
        image_ok = IMAGE_ROOT.is_dir() and os.access(IMAGE_ROOT, os.R_OK) and not os.access(IMAGE_ROOT, os.W_OK)
        skill_ok = SKILL_ROOT.is_dir() and os.access(SKILL_ROOT, os.R_OK) and not os.access(SKILL_ROOT, os.W_OK)
        executable_ok = bool(shutil_which(self.opencode_executable))
        socket_absent = not Path("/var/run/docker.sock").exists()
        ready = bool(not self.ready_error and runtime_ok and result_ok and image_ok and skill_ok and executable_ok and socket_absent)
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "executor": "mememeow-agent-executor",
            "opencode": executable_ok,
            "runtime_read_write": runtime_ok and result_ok,
            "images_read_only": image_ok,
            "skills_read_only": skill_ok,
            "docker_socket_absent": socket_absent,
            "token_configured": bool(self.token),
            "opencode_configured": bool(self.model and self.base_url and self.api_key),
            "capacity": self.max_workers,
            "queued": self._queued_count(),
            "error": self.ready_error,
        }

    def _queued_count(self) -> int:
        """返回尚未开始的任务数量，调用方可在锁外使用。"""
        with self.lock:
            return sum(1 for task in self.tasks.values() if task.status == "queued")

    def _validate_request(self, payload: object) -> tuple[str, str, str, int, bool, str | None]:
        """校验固定任务字段并返回规范化参数。"""
        if not isinstance(payload, dict):
            raise ValueError("invalid_task")
        unknown = set(payload) - ALLOWED_REQUEST_FIELDS
        if unknown:
            raise ValueError("invalid_task")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("agent_result_path_invalid")
        relative = _relative_image_path(payload.get("image_relative_path"))
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
        try:
            timeout = int(payload.get("timeout_seconds", self.max_timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_task") from exc
        if timeout < 1 or timeout > self.max_timeout:
            raise ValueError("agent_timeout_limit_exceeded")
        wait = payload.get("wait", True)
        if not isinstance(wait, bool):
            raise ValueError("invalid_task")
        callback_token = payload.get("callback_token")
        if callback_token is not None and (not isinstance(callback_token, str) or len(callback_token) > 4096):
            raise ValueError("invalid_task")
        return task_id, relative.as_posix(), str(policy), timeout, wait, callback_token

    def submit(self, payload: object) -> tuple[TaskState, bool]:
        """创建固定研究任务并交给受限线程池，返回状态和同步等待标记。"""
        task_id, relative, policy, timeout, wait, callback_token = self._validate_request(payload)
        with self.lock:
            if not self.health().get("ready"):
                raise RuntimeError("agent_runtime_unavailable")
            if not self.model or not self.base_url or not self.api_key:
                raise RuntimeError("opencode_not_configured")
            existing = self.tasks.get(task_id)
            if existing is not None:
                raise RuntimeError("task_exists")
            if self._queued_count() >= self.backpressure:
                raise RuntimeError("agent_backpressure")
            task = TaskState(task_id, relative, policy, timeout, callback_token=callback_token, result_path=f"task-results/{task_id}/{RESULT_FILE_NAME}")
            self.tasks[task_id] = task
            self.futures[task_id] = self.pool.submit(self._run, task)
            return task, wait

    def _task_environment(self, task: TaskState) -> dict[str, str]:
        """构造 OpenCode 最小环境白名单，不继承 executor 容器中的无关变量。"""
        values = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.getenv("HOME", "/runtime/home"),
            "OPENCODE_DB": str(RUNTIME_ROOT / "opencode.db"),
            "OPENCODE_CONFIG": str(WORKSPACE / "opencode.json"),
            "OPENCODE_CONFIG_DIR": str(WORKSPACE / ".opencode"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "MEMEMEOW_OPENCODE_BASE_URL": self.base_url,
            "MEMEMEOW_OPENCODE_API_KEY": self.api_key,
            "MEMEMEOW_OPENCODE_SLOT": "0",
            "MEMEMEOW_AGENT_TASK_ID": task.task_id,
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
        return (
            "使用 research-meme-context skill 分析这张表情包；只通过项目内部接口使用可选反向图片能力。"
            f"本任务 reverse_image_policy={task.reverse_image_policy}。"
            f"结果必须写入 {result_dir / RESULT_FILE_NAME}；先写 {result_dir / RESULT_DRAFT_NAME}，"
            "使用 output-schema.json 校验后通过同一文件系统的原子 rename/mv 替换最终文件。"
            "不要把业务 JSON 作为 assistant 文本交付，不要写入数据库。"
        )

    def _run(self, task: TaskState) -> None:
        """执行单个固定 OpenCode 任务并更新状态，失败时只保存稳定诊断。"""
        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        stderr = b""
        try:
            with self.lock:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                    task.error = _json_error("task_interrupted", "任务已取消")
                    task.completed_at = time.time()
                    task.done.set()
                    return
                task.status = "running"
                task.started_at = time.time()
                result_dir = RESULT_ROOT / task.task_id
                if result_dir.is_symlink() or (result_dir.exists() and not result_dir.is_dir()):
                    raise RuntimeError("agent_result_path_invalid")
                result_dir.mkdir(parents=True, exist_ok=True)
                for filename in (RESULT_FILE_NAME, RESULT_DRAFT_NAME):
                    path = result_dir / filename
                    if path.exists() or path.is_symlink():
                        path.unlink()
            env = self._task_environment(task)
            image = IMAGE_ROOT / _relative_image_path(task.image_relative_path)
            command = [
                self.opencode_executable,
                "run",
                "--auto",
                "--dir",
                str(WORKSPACE),
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
                self._prompt(task),
            ]
            with tempfile.TemporaryFile(dir=LOG_ROOT, prefix=f"{task.task_id}-", mode="w+b") as out, tempfile.TemporaryFile(dir=LOG_ROOT, prefix=f"{task.task_id}-", mode="w+b") as err:
                process = subprocess.Popen(command, cwd=WORKSPACE, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
                with self.lock:
                    task.process = process
                deadline = time.monotonic() + task.timeout_seconds
                while process.poll() is None:
                    if task.cancel_event.is_set():
                        self._terminate(process)
                        raise RuntimeError("task_interrupted")
                    if time.monotonic() >= deadline:
                        self._terminate(process)
                        raise RuntimeError("agent_timeout")
                    time.sleep(0.05)
                out.flush()
                err.flush()
                out.seek(0)
                err.seek(0)
                stdout = out.read(256 * 1024)
                stderr = err.read(16 * 1024)
            if process.returncode != 0:
                raise RuntimeError("agent_process_failed:" + _diagnostic(stdout, stderr))
            for line in stdout.splitlines():
                try:
                    session = _session_id_from_event(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if session:
                    task.session_id = session
            if not task.session_id:
                raise RuntimeError("agent_output_invalid_json")
            self._validate_result_file(RESULT_ROOT / task.task_id / RESULT_FILE_NAME)
            with self.lock:
                if task.cancel_event.is_set():
                    raise RuntimeError("task_interrupted")
                task.status = "succeeded"
                task.completed_at = time.time()
        except RuntimeError as exc:
            code, _, detail = str(exc).partition(":")
            with self.lock:
                task.status = "cancelled" if code == "task_interrupted" else "failed"
                fallback = {
                    "agent_timeout": "OpenCode 执行超时",
                    "task_interrupted": "任务已取消",
                }.get(code, "任务执行失败")
                task.error = _json_error(code, detail[:500] if detail else fallback)
                task.completed_at = time.time()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            with self.lock:
                task.status = "failed"
                task.error = _json_error("agent_process_failed", str(exc)[:500])
                task.completed_at = time.time()
        finally:
            with self.lock:
                task.process = None
                task.done.set()

    def _validate_result_file(self, path: Path) -> None:
        """验证结果文件位置、大小和基本 JSON 结构；完整 schema 由后端复核。"""
        root = RESULT_ROOT.resolve()
        current = RESULT_ROOT
        try:
            relative = path.relative_to(RESULT_ROOT)
        except ValueError as exc:
            raise RuntimeError("agent_result_path_invalid") from exc
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError("agent_result_path_invalid")
        candidate = path.resolve(strict=True)
        candidate.relative_to(root)
        if path.is_symlink() or not candidate.is_file():
            raise RuntimeError("agent_result_file_unreadable")
        if candidate.stat().st_size > self.max_result_bytes:
            raise RuntimeError("agent_result_file_too_large")
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError("agent_result_file_missing") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("agent_result_file_invalid_json") from exc
        if not isinstance(value, dict) or not REQUIRED_RESULT_FIELDS.issubset(value):
            raise RuntimeError("agent_result_file_schema_invalid")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """终止任务进程组，避免取消或超时留下 OpenCode 子进程。"""
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

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
            self._terminate(process)
        return task

    def close(self) -> None:
        """停止线程池并取消仍未结束的任务。"""
        with self.lock:
            ids = [task.task_id for task in self.tasks.values() if task.status not in {"succeeded", "failed", "cancelled"}]
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
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        """处理健康和任务状态查询。"""
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
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
            }
            self._send(400, _json_error(code, messages.get(code, "任务请求无效")))
            return
        except RuntimeError as exc:
            code = str(exc)
            status = {
                "agent_backpressure": 429,
                "task_exists": 409,
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
