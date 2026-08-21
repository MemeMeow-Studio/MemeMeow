"""OpenCode 图片语境生成运行器。

模块只负责准备固定运行目录、调用公开 CLI 并把不可信输出转为候选 JSON；canonical
sidecar 的读取、指纹复核和写入始终由上层元数据服务完成。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, RLock, Semaphore
from typing import Any, BinaryIO, Callable, Iterator
from urllib.error import URLError
from urllib.request import urlopen

from backend.agent_executor import AgentExecutorClient, AgentExecutorError
from backend.agent_resume import classify_resume_error, normalize_identifier
from backend.config import Settings
from backend.metadata import MemeContext
from backend.opencode_workspace import (
    LocalWorkspaceProvider,
    ResolvedWorkspace,
    TrustedWorkspaceContext,
    WorkspaceCapabilityError,
    WorkspaceProvider,
    WorkspaceResolutionError,
    build_edit_permission_rules,
    build_external_directory_rules,
    capability_for_provider,
    validate_directory_path,
    validate_file_path,
)
from backend.public_dto import PublicDataError, secret_inventory_from_settings, validate_agent_result

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - 兼容尚未同步依赖的开发环境
    Draft202012Validator = None
    FormatChecker = None


# 此配置只定义兼容 OpenAI API 的通用 provider，不保存部署地址、长期密钥或模型选择。
# broker 地址和当前任务的短期 capability 由 executor 在子进程环境中装配。
RUNTIME_OPENCODE_CONFIG: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "experimental": {
        "continue_loop_on_deny": True,
    },
    "provider": {
        "mememeow": {
            "npm": "@ai-sdk/openai",
            "name": "MemeMeow OpenAI Responses provider",
            "options": {
                "baseURL": "{env:MEMEMEOW_MODEL_BROKER_URL}",
                "apiKey": "{env:MEMEMEOW_MODEL_CAPABILITY}",
            },
            "models": {
                "gpt-5.6-luna": {
                    "id": "gpt-5.6-luna",
                    "name": "GPT-5.6 Luna",
                    "family": "gpt-luna",
                    "release_date": "2026-07-09",
                    "status": "active",
                    "reasoning": True,
                    "tool_call": True,
                    "temperature": False,
                    "attachment": True,
                    "interleaved": False,
                    "modalities": {
                        "input": ["text", "image", "pdf"],
                        "output": ["text"],
                    },
                    "limit": {
                        "context": 1050000,
                        "input": 922000,
                        "output": 128000,
                    },
                    "cost": {
                        "input": 0.2,
                        "output": 1.2,
                        "cache_read": 0.02,
                        "cache_write": 0.25,
                        "context_over_200k": {
                            "input": 0.4,
                            "output": 1.8,
                            "cache_read": 0.04,
                            "cache_write": 0.5,
                        },
                    },
                    "variants": {
                        "max": {"reasoningEffort": "max"},
                    },
                }
            },
        }
    },
}

# 当前研究任务统一使用 Luna 的 max 变体，避免不同任务因默认推理强度产生质量漂移。
OPENCODE_REASONING_VARIANT = "max"

# 诊断日志只保留固定前缀，避免完整 Agent transcript 长期占用 runtime 磁盘。
# 该值不参与 OpenCode 输出读取或业务结果解析，因此不会拒绝大输出。
DIAGNOSTIC_LOG_BYTES = 256 * 1024
STREAM_COPY_CHUNK_BYTES = 64 * 1024
RESULT_FILE_NAME = "result.json.tmp"
RESULT_DRAFT_NAME = "result.json.draft"
RESULT_DEFAULT_MAX_BYTES = 1024 * 1024
EXECUTOR_IMAGE_ROOT = Path("/images")
_SECRET_PATTERNS = (
    # 分开处理 JSON 字符串值和普通 header/日志格式，保留原有引号结构。
    re.compile(r"(?i)(authorization\s*[\"']?\s*:\s*[\"']?bearer\s+)[^\s,;}\]\"']+"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*:\s*)([\"'])(.*?)\2"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)[\"']?\s*[:=]\s*)[^\s,;}\]\"']+"),
)
_PATH_PATTERN = re.compile(r"(?:/runtime|/images|/skills|/app|[A-Za-z]:\\)[^\s,;]+")
_GENERIC_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s,;:'\"()\[\]{}]+/)+[^\s,;:'\"()\[\]{}]+")


def _redact_runtime_diagnostic(value: str, secrets: tuple[str, ...] = ()) -> str:
    """从 host OpenCode 诊断中移除凭据、路径和多行 transcript。"""
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
    return _GENERIC_PATH_PATTERN.sub("[PATH]", value)[:500]


def _stream_sample(stream: BinaryIO, limit: int) -> bytes:
    """读取临时输出的头尾样本，避免大 JSONL 将 provider 错误留在不可见尾部。"""
    stream.seek(0)
    head = stream.read(limit)
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size <= limit:
        return head
    stream.seek(max(0, size - limit))
    return head + b"\n" + stream.read(limit)


class OpenCodeError(RuntimeError):
    """携带稳定错误码的 OpenCode 运行失败。"""

    def __init__(self, code: str, message: str | None = None, *, session_id: str | None = None, executor_attempt_id: str | None = None, retryable: bool | None = None, http_status: int | None = None):
        super().__init__(message or code)
        self.code = code
        self.session_id = normalize_identifier(session_id, kind="session")
        self.executor_attempt_id = normalize_identifier(executor_attempt_id, kind="attempt")
        self.retryable = retryable
        self.http_status = http_status


def _workspace_opencode_config(workspace: ResolvedWorkspace) -> dict[str, Any]:
    """生成当前 workspace 的无密钥 OpenCode 配置和文件工具权限规则。"""
    config = json.loads(json.dumps(RUNTIME_OPENCODE_CONFIG, ensure_ascii=False))
    config["permission"] = {
        "external_directory": {path: decision for path, decision in workspace.permission_rules},
        "edit": {
            path: decision
            for path, decision in build_edit_permission_rules(
                task_scratch_root=workspace.task_scratch_root,
                config_file=workspace.config_file,
                config_dir=workspace.config_dir,
                draft_path=workspace.draft_path,
                result_path=workspace.result_path,
            )
        },
    }
    return config


class OpenCodeRunner:
    """在固定 runtime 中按 slot 受控并行执行 OpenCode，并返回已校验研究结果。"""

    def __init__(self, settings: Settings, project_root: Path | None = None, *, workspace_provider: WorkspaceProvider | None = None):
        """初始化 runner；应用装配应显式传入 provider，旧直接夹具兼容 local。"""
        self.settings = settings
        self.project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        self.runtime_root = (settings.opencode_runtime_root or settings.data_root / "opencode").expanduser().resolve()
        self.workspace = self.runtime_root / "workspace"
        self.db_path = self.runtime_root / "opencode.db"
        self.slots_root = self.runtime_root / "slots"
        self.lock_path = self.runtime_root / "worker.lock"
        self.log_root = self.runtime_root / "logs"
        self.concurrency = max(1, min(int(settings.opencode_concurrency), 8))
        self._slot_semaphore = Semaphore(self.concurrency)
        self._slot_ids: Queue[int] = Queue()
        for slot_id in range(self.concurrency):
            self._slot_ids.put(slot_id)
        self._prepare_lock = Lock()
        self._runtime_ready = False
        self._closing = Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._processes: set[subprocess.Popen[bytes]] = set()
        # 结果目录创建、活动 task 标记和清理必须共享可重入锁，避免清理与启动交错。
        self._process_lock = RLock()
        self._process_tasks: dict[subprocess.Popen[bytes], str | None] = {}
        self._active_task_ids: set[str] = set()
        self._active_executor_task_ids: set[str] = set()
        self._last_executor_attempt_ids: dict[str, str] = {}
        self._cancelled_task_ids: set[str] = set()
        # 未显式传入 provider 只为既有开源直接构造调用保留 local 兼容；应用工厂
        # 对 non-local scope 必须显式装配 provider，不能在这里猜测路径。
        self.workspace_provider: WorkspaceProvider = workspace_provider or LocalWorkspaceProvider(
            self.runtime_root,
            image_root=self._configured_image_root(),
            skill_root=self.project_root / "skills" / "research-meme-context",
        )
        self._workspace_by_task: dict[str, ResolvedWorkspace] = {}
        self._workspace_selectors: dict[str, str] = {}
        self._workspace_capabilities: dict[str, str] = {}
        self.executor = AgentExecutorClient(
            getattr(settings, "agent_executor_url", None),
            getattr(settings, "agent_executor_token", None),
            timeout=int(getattr(settings, "agent_executor_request_timeout_seconds", 1810)),
        )

    def resolve_workspace(self, context: TrustedWorkspaceContext) -> ResolvedWorkspace:
        """从可信任务上下文解析并缓存一次 workspace 描述。"""
        try:
            resolved = self.workspace_provider.resolve(context)
        except (WorkspaceResolutionError, WorkspaceCapabilityError) as exc:
            raise OpenCodeError(exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - provider 是安全边界，异常统一失败关闭
            raise OpenCodeError("opencode_workspace_invalid", "workspace provider 无法解析任务") from exc
        if not isinstance(resolved, ResolvedWorkspace):
            raise OpenCodeError("opencode_workspace_invalid", "workspace provider 返回值无效")
        if context.selector is not None and context.selector != resolved.selector:
            raise OpenCodeError("opencode_workspace_mismatch", "workspace selector 与可信任务事实不一致")
        try:
            return self._validate_resolved_workspace(context, resolved)
        except WorkspaceResolutionError as exc:
            raise OpenCodeError(exc.code, str(exc)) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise OpenCodeError("opencode_workspace_invalid", "workspace provider 返回值无法校验") from exc

    def _validate_resolved_workspace(self, context: TrustedWorkspaceContext, resolved: ResolvedWorkspace) -> ResolvedWorkspace:
        """固定 provider 输出的结果/DB 布局，并重建服务端权限规则。

        provider 可以把图片、metadata 和 Skill 放在容器外部受控挂载，但配置、Task
        临时目录和结果路径不能变成新的任意文件写入接口。这里的检查只验证已存在
        的输入视图；Task 目录和结果目录在 capability 校验后才创建。
        """
        def absolute(path: Path) -> Path:
            """规范化路径而不跟随最终符号链接。"""
            return Path(os.path.abspath(Path(path).expanduser()))

        directory = absolute(resolved.directory)
        scratch = absolute(resolved.task_scratch_root)
        config_file = absolute(resolved.config_file)
        config_dir = absolute(resolved.config_dir)
        images_root = absolute(resolved.images_root)
        metadata_root = absolute(resolved.metadata_root)
        skill_root = absolute(resolved.skill_root)
        task_results = absolute(resolved.task_results_root)
        expected_results = absolute(self.runtime_root / "task-results" / context.task_id)
        expected_db = absolute(self.db_path)
        if not isinstance(resolved.selector, str) or resolved.selector != resolved.selector.strip() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", resolved.selector):
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace selector 无效")
        if absolute(resolved.db_path) != expected_db:
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace DB 路径不受支持")
        validate_file_path(resolved.db_path, allow_missing=True, code="opencode_workspace_invalid")
        if task_results != expected_results:
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace 结果路径不受支持")
        if scratch != directory / "tasks" / context.task_id:
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace 临时路径不受支持")
        if config_file != scratch / "opencode.json" or config_dir != scratch / ".opencode":
            raise WorkspaceResolutionError("opencode_workspace_invalid", "workspace 配置路径不受支持")
        if resolved.local and context.scope_id != "local":
            raise WorkspaceResolutionError("opencode_workspace_mismatch", "non-local 任务不能使用 local workspace")
        if resolved.local and resolved.selector != "local":
            raise WorkspaceResolutionError("opencode_workspace_invalid", "local workspace selector 无效")
        if not resolved.local and resolved.selector == "local":
            raise WorkspaceResolutionError("opencode_workspace_invalid", "external workspace selector 保留字无效")
        for path in (directory, images_root, metadata_root):
            validate_directory_path(path, code="opencode_workspace_invalid")
        if resolved.local and skill_root.is_symlink():
            # local provider 为既有 checkout 保留相对 Skill 链接；链接目标仍须是普通目录。
            validate_directory_path(skill_root.resolve(strict=True), code="opencode_workspace_invalid")
        else:
            validate_directory_path(skill_root, code="opencode_workspace_invalid")
        for path in (scratch, config_dir, task_results):
            if path.exists() or path.is_symlink():
                validate_directory_path(path, code="opencode_workspace_invalid")
        validated = replace(
            resolved,
            directory=directory,
            config_file=config_file,
            config_dir=config_dir,
            images_root=images_root,
            metadata_root=metadata_root,
            skill_root=skill_root,
            task_scratch_root=scratch,
            task_results_root=task_results,
            db_path=expected_db,
        )
        return replace(validated, _permission_rules=build_external_directory_rules(validated))

    def workspace_for_task(self, task_id: str) -> str | None:
        """返回任务最近解析的 opaque selector，供诊断写回。"""
        with self._process_lock:
            return self._workspace_selectors.get(task_id)

    @property
    def executor_mode(self) -> bool:
        """判断当前任务是否通过已选定的 executor 执行。"""
        mode = getattr(self.settings, "agent_runtime_mode", "auto")
        if mode not in {"auto", "executor", "host"}:
            raise OpenCodeError("agent_runtime_mode_invalid", "Agent 运行模式不受支持")
        if mode == "executor":
            return True
        return mode == "auto" and self.executor.configured

    @property
    def last_executor_attempt_id(self) -> str | None:
        """返回最近一次当前线程任务的 executor attempt 摘要。"""
        # 该属性只作为成功写回的诊断补充；真正的绑定事实仍来自 executor 响应。
        with self._process_lock:
            if not self._last_executor_attempt_ids:
                return None
            return next(reversed(self._last_executor_attempt_ids.values()))

    def executor_attempt_id_for(self, task_id: str) -> str | None:
        """按业务 task 读取最近 attempt，避免并发任务互相串接诊断。"""
        with self._process_lock:
            return self._last_executor_attempt_ids.get(task_id)

    def _remember_executor_attempt(self, task_id: str, executor_attempt_id: str) -> None:
        """保存任务 attempt 摘要并限制长期运行进程的内存历史。"""
        with self._process_lock:
            self._last_executor_attempt_ids[task_id] = executor_attempt_id
            while len(self._last_executor_attempt_ids) > 5000:
                self._last_executor_attempt_ids.pop(next(iter(self._last_executor_attempt_ids)))

    @staticmethod
    def _executor_error_code(code: str, *, health: bool = False) -> str:
        """把 executor 客户端异常收口为任务服务可识别的稳定错误码。"""
        if health and code in {"agent_timeout", "agent_executor_unavailable", "agent_executor_http_error"}:
            return "agent_runtime_unavailable"
        known = {
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
            "agent_executor_not_configured",
            "agent_executor_unavailable",
            "agent_executor_unauthorized",
            "agent_executor_invalid_response",
            "opencode_workspace_invalid",
            "opencode_workspace_mismatch",
            "opencode_workspace_capability_invalid",
            "opencode_workspace_capability_expired",
            "opencode_workspace_capability_unavailable",
            "opencode_workspace_provider_missing",
        }
        return code if code in known else "agent_executor_invalid_response"

    def _configured_image_root(self) -> Path:
        """解析设置中的图片根目录，统一相对路径基准以匹配 Compose 挂载。"""
        configured = Path(self.settings.image_root or self.project_root / "data" / "images").expanduser()
        if not configured.is_absolute():
            configured = self.project_root / configured
        return configured.resolve()

    def _link(self, target: Path, source: Path) -> None:
        """创建或核验相对链接，拒绝覆盖不属于 runtime 的真实目录。"""
        if target.is_symlink():
            if target.resolve() == source.resolve():
                return
            target.unlink()
        elif target.exists():
            raise OpenCodeError("opencode_not_configured", f"runtime 路径冲突：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.path.relpath(source, target.parent), target_is_directory=True)

    def _config_for_runtime(self, workspace: ResolvedWorkspace | None = None) -> dict[str, Any]:
        """生成当前运行模式的无密钥配置。

        生产 executor 只使用 broker/capability 占位符；local/host 夹具继续使用
        旧 endpoint/key 环境引用，保证开发模式与已安装 OpenCode 的兼容回归。
        """
        config = _workspace_opencode_config(workspace) if workspace is not None else json.loads(json.dumps(RUNTIME_OPENCODE_CONFIG, ensure_ascii=False))
        production_executor = str(getattr(self.settings, "public_release_profile", "local")).casefold() in {"production", "public", "1", "true", "yes", "on"} and self.executor_mode
        if not production_executor:
            options = config.setdefault("provider", {}).setdefault("mememeow", {}).setdefault("options", {})
            options["baseURL"] = "{env:MEMEMEOW_OPENCODE_BASE_URL}"
            options["apiKey"] = "{env:MEMEMEOW_OPENCODE_API_KEY}"
        return config

    def _write_workspace_config(self, workspace: ResolvedWorkspace) -> None:
        """原子写入当前 workspace 的无密钥配置和外部目录权限。"""
        target = workspace.config_file
        validate_directory_path(target.parent, create=True, code="opencode_workspace_invalid")
        validate_file_path(target, allow_missing=True, code="opencode_workspace_invalid")
        content = json.dumps(self._config_for_runtime(workspace), ensure_ascii=False, indent=2) + "\n"
        try:
            if target.read_text(encoding="utf-8") == content:
                os.chmod(target, 0o600)
                return
        except FileNotFoundError:
            pass
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}.{id(self)}")
        validate_file_path(temporary, allow_missing=True, code="opencode_workspace_invalid")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(content)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temporary, target)

    def _write_runtime_config(self) -> None:
        """兼容旧诊断入口，写入既有 local workspace 基础配置。"""
        target = self.workspace / "opencode.json"
        content = json.dumps(self._config_for_runtime(), ensure_ascii=False, indent=2) + "\n"
        try:
            if target.read_text(encoding="utf-8") == content:
                return
        except FileNotFoundError:
            pass
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}.{id(self)}")
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    def prepare_runtime(self) -> None:
        """准备可复用 workspace，不执行任何包管理器或依赖下载。"""
        with self._prepare_lock:
            if self._runtime_ready:
                return
            if not self.settings.opencode_model:
                raise OpenCodeError("opencode_not_configured", "未配置 OpenCode 模型")
            production = str(getattr(self.settings, "public_release_profile", "local")).casefold() in {"production", "public", "1", "true", "yes", "on"}
            if production and self.executor_mode:
                if not getattr(self.settings, "model_broker_url", None):
                    raise OpenCodeError("model_broker_endpoint_invalid", "生产模型 broker 未配置")
            elif not self.settings.opencode_base_url or not self.settings.opencode_api_key:
                raise OpenCodeError("opencode_not_configured", "未配置 OpenCode 服务地址或密钥")
            if self.executor_mode:
                if not self.executor.configured:
                    raise OpenCodeError("agent_executor_not_configured", "Agent executor 地址或凭据 token 未配置")
                try:
                    health = self.executor.health()
                except AgentExecutorError as exc:
                    # 健康探针失败也必须进入任务稳定错误协议，不能把客户端异常
                    # 直接交给长任务服务并退化成 task_failed。
                    code = self._executor_error_code(exc.code, health=True)
                    raise OpenCodeError(code, str(exc)[:500]) from exc
                if not bool(health.get("ready")):
                    raise OpenCodeError("agent_runtime_unavailable", "Agent executor 健康检查未通过")
            else:
                executable = self.settings.opencode_executable
                if not executable or (not Path(executable).is_file() and shutil.which(executable) is None):
                    raise OpenCodeError("opencode_not_configured", "未找到 OpenCode 可执行文件")
                skills_source = self.project_root / "skills" / "research-meme-context"
                if not isinstance(self.workspace_provider, LocalWorkspaceProvider):
                    # 外部 provider 已在 resolve 阶段验证自己的只读 Skill 视图，
                    # host 模式不应再要求项目根存在同一份副本。
                    skills_source = None
                # 项目自己的 OpenCode 插件依赖与前端依赖隔离；环境变量可覆盖这一默认共享目录。
                shared_modules = self.settings.opencode_node_modules or self.project_root / ".opencode" / "node_modules"
                if (skills_source is not None and not skills_source.is_dir()) or not shared_modules.is_dir() or not (shared_modules / "@ai-sdk" / "openai").is_dir():
                    raise OpenCodeError("opencode_not_configured", "OpenCode skill、共享 node_modules 或 Responses provider 未预先安装")
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            (self.runtime_root / "home").mkdir(parents=True, exist_ok=True)
            if isinstance(self.workspace_provider, LocalWorkspaceProvider):
                self.workspace.mkdir(parents=True, exist_ok=True)
            self.slots_root.mkdir(parents=True, exist_ok=True)
            self.log_root.mkdir(parents=True, exist_ok=True)
            if isinstance(self.workspace_provider, LocalWorkspaceProvider):
                self._write_runtime_config()
            if not self.executor_mode and isinstance(self.workspace_provider, LocalWorkspaceProvider):
                skills_source = self.project_root / "skills" / "research-meme-context"
                self._link(self.workspace / ".opencode" / "skills" / "research-meme-context", skills_source)
                self._link(self.workspace / "node_modules", shared_modules)
            self._runtime_ready = True

    def runtime_probe(self) -> dict[str, object]:
        """返回共享 runtime、skill 和依赖探针结果，供启动诊断使用。"""
        if self.executor_mode:
            error_code: str | None = None
            try:
                health = self.executor.health()
            except AgentExecutorError as exc:
                health = {}
                error_code = self._executor_error_code(exc.code, health=True)
            ready = bool(health.get("ready"))
            runtime_ready = bool(health.get("runtime_read_write"))
            images_ready = bool(health.get("images_read_only"))
            skills_ready = bool(health.get("skills_read_only"))
            executable_ready = bool(health.get("opencode"))
            socket_absent = bool(health.get("docker_socket_absent"))
            return {
                "mode": "executor",
                "executor_url_configured": bool(self.executor.url),
                "executor_token_configured": bool(self.executor.token),
                "executor_running": ready,
                "runtime_root_ready": self.runtime_root.is_dir(),
                "workspace_ready": self.workspace.is_dir(),
                "executable_ready": executable_ready,
                "skills_ready": skills_ready,
                "dependencies_ready": executable_ready,
                "mounts_ready": runtime_ready and images_ready and skills_ready,
                "runtime_read_write": runtime_ready,
                "images_read_only": images_ready,
                "skill_read_only": skills_ready,
                "non_root": True,
                "network_ready": ready,
                "docker_socket_absent": socket_absent,
                "concurrency": self.concurrency,
                "error_code": error_code,
                "verified": bool(ready and self.executor.configured and runtime_ready and images_ready and skills_ready and executable_ready and socket_absent),
            }
        executable = self.settings.opencode_executable
        executable_ready = bool(executable and (Path(executable).is_file() or shutil.which(executable)))
        skills_ready = (self.project_root / "skills" / "research-meme-context").is_dir()
        modules = self.settings.opencode_node_modules or self.project_root / ".opencode" / "node_modules"
        dependencies_ready = modules.is_dir() and (modules / "@ai-sdk" / "openai").is_dir()
        return {
            "runtime_root_ready": self.runtime_root.is_dir(),
            "workspace_ready": self.workspace.is_dir(),
            "db_path": self.db_path.name,
            "executable_ready": executable_ready,
            "skills_ready": skills_ready,
            "dependencies_ready": dependencies_ready,
            "concurrency": self.concurrency,
            "mode": "host-runtime-slot-lock",
            "verified": bool(executable_ready and skills_ready and dependencies_ready),
        }

    def probe_runtime(self) -> dict[str, object]:
        """提供兼容命名的 runtime 探针入口，供诊断脚本和设置页调用。"""
        return self.runtime_probe()

    def skill_hash(self) -> str:
        """计算 skill 文件内容哈希，用于把运行时契约写入任务记录。"""
        source = self.project_root / "skills" / "research-meme-context"
        digest = hashlib.sha256()
        for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
            digest.update(path.relative_to(source).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _agent_reverse_image_url(self) -> str:
        """返回 Agent 使用的反向图片回调地址，兼容旧版设置夹具。"""
        return str(getattr(self.settings, "agent_reverse_image_internal_url", None) or self.settings.reverse_image_internal_url)

    def _agent_visual_search_url(self) -> str:
        """返回 Agent 使用的 task-scoped 视觉匹配回调地址，兼容旧版设置夹具。"""
        configured = getattr(self.settings, "agent_visual_search_internal_url", None)
        if configured:
            return str(configured)
        visual_search = getattr(self.settings, "visual_search_internal_url", None)
        if visual_search:
            return str(visual_search)
        return str(self.settings.visual_internal_url).replace("/visual-embedding", "/visual-search/match")

    def build_environment(
        self,
        slot_id: int | None = None,
        task_id: str | None = None,
        callback_token: str | None = None,
        *,
        workspace: ResolvedWorkspace | None = None,
    ) -> dict[str, str]:
        """构造隔离的 OpenCode 进程环境，供后台任务和交互检查入口共同使用。"""
        active_workspace = workspace or ResolvedWorkspace(
            selector="local",
            directory=self.workspace,
            config_file=self.workspace / "opencode.json",
            config_dir=self.workspace / ".opencode",
            images_root=self._configured_image_root(),
            metadata_root=self._configured_image_root(),
            skill_root=self.project_root / "skills" / "research-meme-context",
            task_scratch_root=self.workspace / "tasks" / (task_id or "interactive"),
            task_results_root=self.runtime_root / "task-results" / (task_id or "interactive"),
            db_path=self.db_path,
            local=True,
        )
        if self.executor_mode:
            # API 不启动 OpenCode 子进程；该快照仅供诊断，绝不把 executor token、
            # callback 根 secret 或长期模型凭据传给 Agent。
            values = {
                "OPENCODE_DB": "/runtime/opencode.db",
                "OPENCODE_CONFIG": str(active_workspace.config_file),
                "OPENCODE_CONFIG_DIR": str(active_workspace.config_dir),
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL": self._agent_reverse_image_url(),
                "MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL": self._agent_visual_search_url(),
                "MEMEMEOW_DATA_ROOT": "/runtime",
            }
            if slot_id is not None:
                values["MEMEMEOW_OPENCODE_SLOT"] = str(slot_id)
            if task_id:
                values["MEMEMEOW_AGENT_TASK_ID"] = str(task_id)
            if callback_token:
                values["MEMEMEOW_AGENT_CALLBACK_TOKEN"] = callback_token
            return values
        # host 回滚模式也必须使用白名单；继承整个 API 环境会把 callback
        # 根 secret、executor token、数据库配置或其它 scope 配置带进 Agent 子进程。
        runtime_root = self.runtime_root
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            # 使用 runtime 专属 HOME，避免 host 兼容模式读取调用用户的凭据目录。
            "HOME": str(self.runtime_root / "home"),
            "OPENCODE_DB": str(runtime_root / "opencode.db"),
            "OPENCODE_CONFIG": str(active_workspace.config_file),
            "OPENCODE_CONFIG_DIR": str(active_workspace.config_dir),
            # 禁止向上合并项目根配置，避免任务意外使用其他 provider 或本地凭据。
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "MEMEMEOW_DATA_ROOT": str(runtime_root),
            "MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT": str(runtime_root / "reverse_image_cache" / "serpapi_google_lens"),
            "MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL": self._agent_reverse_image_url(),
            # 视觉 Skill 只接收 task-scoped 内部地址，不获得数据库或模型运行时权限。
            "MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL": self._agent_visual_search_url(),
        }
        environment["MEMEMEOW_OPENCODE_BASE_URL"] = str(self.settings.opencode_base_url or "")
        environment["MEMEMEOW_OPENCODE_API_KEY"] = str(self.settings.opencode_api_key or "")
        shared_modules = self.settings.opencode_node_modules or self.project_root / ".opencode" / "node_modules"
        environment["NODE_PATH"] = str(shared_modules)
        if slot_id is not None:
            environment["MEMEMEOW_OPENCODE_SLOT"] = str(slot_id)
        if task_id:
            environment["MEMEMEOW_AGENT_TASK_ID"] = str(task_id)
        if callback_token:
            environment["MEMEMEOW_AGENT_CALLBACK_TOKEN"] = callback_token
        return environment

    def _acquire_slot(self) -> tuple[int, Any]:
        """获取进程内 semaphore 和跨进程 slot 文件锁。"""
        acquired = False
        while not self._closing.is_set():
            if self._slot_semaphore.acquire(timeout=0.2):
                acquired = True
                break
        if not acquired or self._closing.is_set():
            # 关闭信号可能与 semaphore 获取同时到达，不能遗留已占用配额。
            if acquired:
                self._slot_semaphore.release()
            raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows 退化为进程内互斥
            if self._closing.is_set():
                self._slot_semaphore.release()
                raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭")
            try:
                slot_id = self._slot_ids.get(timeout=0.2)
            except Empty as exc:
                self._slot_semaphore.release()
                raise OpenCodeError("opencode_slot_unavailable", "无法获取 OpenCode slot") from exc
            if self._closing.is_set():
                self._slot_ids.put(slot_id)
                self._slot_semaphore.release()
                raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭")
            return slot_id, None
        while not self._closing.is_set():
            try:
                slot_id = self._slot_ids.get(timeout=0.2)
            except Empty:
                continue
            if self._closing.is_set():
                self._slot_ids.put(slot_id)
                self._slot_semaphore.release()
                raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭")
            lock_path = self.slots_root / f"slot-{slot_id}.lock"
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = lock_path.open("a+")
            except OSError as exc:
                self._slot_ids.put(slot_id)
                self._slot_semaphore.release()
                raise OpenCodeError("opencode_slot_unavailable", "无法创建 OpenCode slot 锁") from exc
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                self._slot_ids.put(slot_id)
                time.sleep(0.02)
                continue
            except OSError as exc:
                handle.close()
                self._slot_ids.put(slot_id)
                self._slot_semaphore.release()
                raise OpenCodeError("opencode_slot_unavailable", "无法获取 OpenCode slot 锁") from exc
            return slot_id, handle
        self._slot_semaphore.release()
        raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭")

    def _release_slot(self, slot_id: int, handle: Any) -> None:
        """释放跨进程 slot 文件锁和进程内 semaphore。"""
        if handle is not None:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()
        self._slot_ids.put(slot_id)
        self._slot_semaphore.release()

    def _track_process(self, process: subprocess.Popen[bytes], task_id: str | None = None) -> None:
        """登记由当前 runner 管理的进程组，便于并行 shutdown。"""
        with self._process_lock:
            self._process = process
            self._processes.add(process)
            self._process_tasks[process] = task_id

    def _mark_task_active(self, task_id: str) -> None:
        """登记当前进程仍在执行的 task，结果清理时保护其专属目录。"""
        with self._process_lock:
            self._active_task_ids.add(task_id)

    def _unmark_task_active(self, task_id: str) -> None:
        """移除已结束 task 的活动标记，允许后续保留策略清理产物。"""
        with self._process_lock:
            self._active_task_ids.discard(task_id)

    def _untrack_process(self, process: subprocess.Popen[bytes]) -> None:
        """从受管理进程集合移除已收束进程。"""
        with self._process_lock:
            self._processes.discard(process)
            self._process_tasks.pop(process, None)
            self._process = next(iter(self._processes), None)

    def _take_pre_cancelled(self, task_id: str) -> bool:
        """消费任务开始前收到的取消标记，阻止取消竞态继续启动外部进程。"""
        with self._process_lock:
            if task_id not in self._cancelled_task_ids:
                return False
            self._cancelled_task_ids.discard(task_id)
            return True

    @staticmethod
    def _event_session(event: object) -> str | None:
        """从公开 JSONL 事件中递归找出 session 标识。"""
        if isinstance(event, dict):
            for key in ("session_id", "sessionID", "sessionId", "id"):
                value = event.get(key)
                if isinstance(value, str) and value and (key != "id" or "session" in str(event.get("type", "")).lower()):
                    return value
            for value in event.values():
                found = OpenCodeRunner._event_session(value)
                if found:
                    return found
        elif isinstance(event, list):
            for value in event:
                found = OpenCodeRunner._event_session(value)
                if found:
                    return found
        return None

    @staticmethod
    def _process_error_diagnostic(stdout: bytes, stderr: bytes, *, secrets: tuple[str, ...] = ()) -> str:
        """从 CLI 的 stderr 或 JSONL error 事件提取有限且可展示的诊断。"""
        return OpenCodeRunner._process_error_diagnostic_stream(BytesIO(stdout), BytesIO(stderr), secrets=secrets)

    @staticmethod
    def _classify_process_failure(stdout: BinaryIO, stderr: BinaryIO) -> str:
        """按有限错误事件区分 provider、网络和普通进程失败。"""
        raw = _stream_sample(stdout, STREAM_COPY_CHUNK_BYTES * 4)
        raw += _stream_sample(stderr, STREAM_COPY_CHUNK_BYTES)
        text = raw.decode("utf-8", errors="replace").lower()
        statuses = [int(match) for match in re.findall(r"(?:statuscode|status|http)[^0-9]{0,8}(\d{3})", text)]
        if any(status == 429 for status in statuses):
            return "agent_provider_rate_limited"
        if any(500 <= status <= 599 for status in statuses):
            return "agent_provider_server_error"
        if any(token in text for token in ("econnreset", "connection reset", "connection refused", "socket hang up", "network error", "timed out")):
            return "agent_connection_interrupted"
        return "agent_process_failed"

    @staticmethod
    def _process_http_status(stdout: BinaryIO, stderr: BinaryIO) -> int | None:
        """从有限进程输出提取 provider HTTP 状态，供脱敏错误摘要使用。"""
        raw = _stream_sample(stdout, STREAM_COPY_CHUNK_BYTES * 4)
        raw += _stream_sample(stderr, STREAM_COPY_CHUNK_BYTES)
        text = raw.decode("utf-8", errors="replace").lower()
        statuses = [int(match) for match in re.findall(r"(?:statuscode|status|http)[^0-9]{0,8}(\d{3})", text)]
        return next((status for status in statuses if status == 429 or 500 <= status <= 599), None)

    @staticmethod
    def _process_error_diagnostic_stream(stdout: BinaryIO, stderr: BinaryIO, *, secrets: tuple[str, ...] = ()) -> str:
        """从临时输出文件提取诊断，读取量受控且不影响完整输出处理。"""
        stderr.seek(0)
        stderr_text = stderr.read(STREAM_COPY_CHUNK_BYTES).decode("utf-8", errors="replace").strip()
        if stderr_text:
            return _redact_runtime_diagnostic(stderr_text, secrets)
        stdout.seek(0)
        diagnostic: str | None = None
        for line in stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "error":
                continue
            error = event.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            if not isinstance(data, dict):
                continue
            message = data.get("message")
            status = data.get("statusCode")
            if isinstance(message, str) and message.strip():
                suffix = f" (HTTP {status})" if isinstance(status, int) else ""
                diagnostic = _redact_runtime_diagnostic(f"{message.strip()}{suffix}", secrets)
        return diagnostic[:500] if diagnostic else "OpenCode 进程以非零状态退出"

    @staticmethod
    def _last_assistant_text(exported: object) -> str:
        """仅抽取完成 session 中最后一条 assistant 消息的 text parts。"""
        messages: list[object] | None = None
        if isinstance(exported, dict):
            for key in ("messages", "data"):
                value = exported.get(key)
                if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                    messages = value
                    break
        elif isinstance(exported, list):
            messages = exported
        if not messages:
            raise OpenCodeError("agent_output_invalid_json", "完成 session 中不存在消息")
        def message_role(item: dict[str, Any]) -> str:
            info = item.get("info")
            role = item.get("role")
            if role is None and isinstance(info, dict):
                role = info.get("role")
            return str(role or "").lower()

        candidates = [item for item in messages if isinstance(item, dict) and message_role(item) == "assistant"]
        if not candidates:
            raise OpenCodeError("agent_output_invalid_json", "完成 session 中不存在 assistant 消息")
        message = candidates[-1]
        parts = message.get("parts", message.get("content", []))
        if isinstance(parts, str):
            return parts
        if not isinstance(parts, list):
            raise OpenCodeError("agent_output_invalid_json", "assistant 消息不包含文本部分")
        texts: list[str] = []
        for part in parts:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and str(part.get("type", "text")).lower() == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if not texts:
            raise OpenCodeError("agent_output_invalid_json", "assistant 消息不包含文本")
        return "".join(texts)

    @staticmethod
    def extract_candidate(text: str) -> dict[str, Any]:
        """严格解析唯一业务 JSON，仅兼容一个纯 JSON fenced block。"""
        raw = text.strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            match = re.fullmatch(r"```json\s*\n?([\s\S]*?)\n?```", raw, flags=re.IGNORECASE)
            if not match:
                raise OpenCodeError("agent_output_invalid_json", "assistant 输出不是唯一 JSON 对象") from None
            try:
                value = json.loads(match.group(1).strip())
            except json.JSONDecodeError as exc:
                raise OpenCodeError("agent_output_invalid_json", "JSON fenced block 无法解析") from exc
        if not isinstance(value, dict):
            raise OpenCodeError("agent_output_invalid_json", "候选输出必须是 JSON 对象")
        return value

    def validate_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """执行输出 schema、字段白名单和敏感数据边界校验。"""
        required = {"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"}
        try:
            candidate = validate_agent_result(candidate, secret_inventory=secret_inventory_from_settings(self.settings))
        except PublicDataError as exc:
            raise OpenCodeError("agent_output_schema_invalid", "候选 JSON 超出公开结果边界") from exc
        if not required.issubset(candidate):
            raise OpenCodeError("agent_output_schema_invalid", "候选 JSON 缺少必填字段")
        schema_path = self.project_root / "skills" / "research-meme-context" / "references" / "output-schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            if Draft202012Validator is not None:
                errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(candidate))
                if errors:
                    raise ValueError(errors[0].message)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OpenCodeError("agent_output_schema_invalid", "候选 JSON 不符合输出 schema") from exc
        try:
            context = MemeContext.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001
            raise OpenCodeError("agent_output_schema_invalid", "候选 JSON 字段不符合约束") from exc
        return context.model_dump(mode="json", exclude_none=False)

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """终止任务独占的进程组，避免超时后留下子进程。"""
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except OSError:
                pass
            except subprocess.TimeoutExpired:
                # 无法继续等待时由上层任务状态收束，避免关闭流程无限阻塞。
                pass

    def _terminate_task(self, task_id: str | None) -> None:
        """终止宿主模式下属于指定 task 的本地进程组，不影响其它任务。"""
        if not task_id:
            return
        with self._process_lock:
            processes = [process for process, active_task_id in self._process_tasks.items() if active_task_id == task_id]
        for process in processes:
            self._terminate(process)

    @staticmethod
    def _iter_json_array_values(source: BinaryIO) -> Iterator[object]:
        """增量解析顶层 JSON 数组，每次只在内存保留一个元素。"""
        decoder = json.JSONDecoder()
        text_source = io.TextIOWrapper(source, encoding="utf-8")
        buffer = ""
        position = 0
        eof = False

        def fill() -> bool:
            """补充固定大小文本块，避免读取器一次申请完整响应。"""
            nonlocal buffer, eof
            if eof:
                return False
            chunk = text_source.read(STREAM_COPY_CHUNK_BYTES)
            if not chunk:
                eof = True
                return False
            buffer += chunk
            return True

        def compact() -> None:
            """丢弃已消费前缀，避免增量缓冲区随元素数量增长。"""
            nonlocal buffer, position
            if position:
                buffer = buffer[position:]
                position = 0

        def skip_whitespace() -> None:
            """定位下一个 JSON 符号。"""
            nonlocal position
            while True:
                while position < len(buffer) and buffer[position].isspace():
                    position += 1
                if position < len(buffer) or eof or not fill():
                    return

        def consume(expected: str) -> None:
            """消费一个已知 JSON 符号并校验结构。"""
            nonlocal position
            skip_whitespace()
            if position >= len(buffer):
                raise json.JSONDecodeError("JSON 响应提前结束", buffer, position)
            if buffer[position] != expected:
                raise json.JSONDecodeError("JSON 数组结构无效", buffer, position)
            position += 1
            compact()

        def decode_value() -> object:
            """解析单个数组元素；跨块时继续填充而不读取后续元素。"""
            nonlocal position
            while True:
                skip_whitespace()
                try:
                    value, end = decoder.raw_decode(buffer, position)
                except json.JSONDecodeError as exc:
                    incomplete = exc.msg.startswith("Unterminated string") or exc.pos >= len(buffer) - 1
                    if eof or not incomplete or not fill():
                        raise
                    continue
                position = end
                compact()
                return value

        skip_whitespace()
        consume("[")
        while True:
            skip_whitespace()
            if position >= len(buffer):
                raise json.JSONDecodeError("JSON 数组提前结束", buffer, position)
            if buffer[position] == "]":
                consume("]")
                skip_whitespace()
                if position < len(buffer) or not eof and fill():
                    skip_whitespace()
                if position < len(buffer):
                    raise json.JSONDecodeError("JSON 响应包含额外内容", buffer, position)
                return
            yield decode_value()
            skip_whitespace()
            if position >= len(buffer):
                raise json.JSONDecodeError("JSON 数组缺少分隔符", buffer, position)
            if buffer[position] == ",":
                position += 1
                compact()
                continue
            if buffer[position] == "]":
                continue
            raise json.JSONDecodeError("JSON 数组缺少分隔符", buffer, position)

    def _session_messages(
        self,
        session_id: str,
        environment: dict[str, str],
        task_id: str | None = None,
        *,
        workspace: ResolvedWorkspace | None = None,
    ) -> object:
        """经临时 loopback server 读取完整 session，规避 export 内联附件的 CLI 截断。"""
        self.log_root.mkdir(parents=True, exist_ok=True)
        workspace_directory = workspace.directory if workspace is not None else self.workspace
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(
            [
                str(self.settings.opencode_executable),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=workspace_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._track_process(server, task_id)
        endpoint = f"http://127.0.0.1:{port}/session/{session_id}/message"
        deadline = time.monotonic() + min(15, self.settings.opencode_timeout_seconds)
        try:
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise OpenCodeError("agent_export_failed", "OpenCode session server 未能启动")
                try:
                    # 响应先流式落到临时文件，避免把完整 transcript 一次性放入内存。
                    with tempfile.TemporaryFile(mode="w+b", prefix="session-", dir=self.log_root) as payload:
                        with urlopen(endpoint, timeout=1) as response:  # noqa: S310 - endpoint 固定为本机 loopback。
                            shutil.copyfileobj(response, payload, length=STREAM_COPY_CHUNK_BYTES)
                        payload.seek(0)
                        first = payload.read(1)
                        payload.seek(0)
                        if first == b"[":
                            # API 返回数组时只保留最后一条 assistant，丢弃大型工具消息。
                            assistant_messages: list[dict[str, Any]] = []
                            for item in self._iter_json_array_values(payload):
                                if not isinstance(item, dict):
                                    continue
                                role = item.get("role")
                                info = item.get("info")
                                if role is None and isinstance(info, dict):
                                    role = info.get("role")
                                if str(role or "").lower() == "assistant":
                                    assistant_messages = [item]
                            return {"messages": assistant_messages}
                        return json.load(payload)
                except OpenCodeError:
                    raise
                except (OSError, TimeoutError, URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            raise OpenCodeError("agent_export_failed", "无法读取 OpenCode session 消息")
        finally:
            self._terminate(server)
            self._untrack_process(server)

    def map_image_path(self, image: Path) -> Path:
        """把后端图片路径映射到 executor 的只读 `/images`，拒绝根目录外文件。"""
        root = self._configured_image_root()
        if self.executor_mode and root != (self.project_root / "data" / "images").resolve():
            raise OpenCodeError("agent_image_root_mismatch", "executor 图片挂载与图片根配置不一致")
        raw_candidate = image.expanduser()
        # 先检查原始路径；若先 resolve，指向图片根目录内的符号链接会失去可识别性。
        if raw_candidate.is_symlink():
            raise OpenCodeError("agent_image_path_forbidden", "图片路径不可作为符号链接读取")
        candidate = raw_candidate.resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise OpenCodeError("agent_image_path_forbidden", "图片路径不在受控图片目录内") from exc
        if not candidate.is_file():
            raise OpenCodeError("agent_image_path_forbidden", "图片路径不可作为普通文件读取")
        return EXECUTOR_IMAGE_ROOT / Path(relative.as_posix()) if self.executor_mode else candidate

    def map_host_image_path(self, image: Path) -> Path:
        """兼容诊断和测试入口，返回图片在当前运行时中的路径。"""
        return self.map_image_path(image)

    def task_result_paths(self, task_id: str, *, workspace: ResolvedWorkspace | None = None) -> tuple[Path, Path]:
        """创建任务专属结果目录并返回草稿、最终临时 JSON 路径。"""
        with self._process_lock:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id or ""):
                raise OpenCodeError("agent_result_path_invalid", "任务结果标识非法")
            root = workspace.task_results_root.parent if workspace is not None else self.runtime_root / "task-results"
            self._ensure_no_symlink_path(self.runtime_root.parent, create=True)
            self._ensure_no_symlink_path(self.runtime_root, create=True)
            self._ensure_no_symlink_path(root, create=True)
            directory = root / task_id
            self._ensure_no_symlink_path(directory, create=True)
            self._ensure_no_symlink_path(directory / RESULT_DRAFT_NAME, allow_file=True)
            self._ensure_no_symlink_path(directory / RESULT_FILE_NAME, allow_file=True)
            return directory / RESULT_DRAFT_NAME, directory / RESULT_FILE_NAME

    @staticmethod
    def _ensure_no_symlink_path(path: Path, *, create: bool = False, allow_file: bool = False) -> None:
        """逐级使用 lstat 检查 runtime 结果路径，拒绝符号链接和节点类型劫持。"""
        current = Path(path.anchor) if path.is_absolute() else Path()
        parts = path.parts[1:] if path.is_absolute() else path.parts
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    return
                try:
                    current.mkdir()
                except FileExistsError:
                    # 目录创建与攻击者替换可能交错，重新 lstat 后继续统一类型校验。
                    try:
                        info = current.lstat()
                    except OSError as exc:
                        raise OpenCodeError("agent_result_path_invalid", "结果路径无法检查") from exc
                except OSError as exc:
                    raise OpenCodeError("agent_result_path_invalid", "结果目录无法创建") from exc
                else:
                    continue
            except OSError as exc:
                raise OpenCodeError("agent_result_path_invalid", "结果路径无法检查") from exc
            if stat.S_ISLNK(info.st_mode):
                raise OpenCodeError("agent_result_path_invalid", "结果路径包含符号链接")
            if current == path and allow_file and stat.S_ISREG(info.st_mode):
                return
            if not stat.S_ISDIR(info.st_mode):
                raise OpenCodeError("agent_result_path_invalid", "结果路径包含非目录节点")

    @staticmethod
    def _reset_task_result_files(draft_path: Path, result_path: Path) -> None:
        """清理本次重试的确定结果文件，避免沿用上一次尝试的成功产物。"""
        OpenCodeRunner._ensure_no_symlink_path(draft_path.parent)
        for path in (draft_path, result_path):
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise OpenCodeError("agent_result_path_invalid", "结果文件路径包含符号链接")
                if stat.S_ISDIR(info.st_mode):
                    raise OpenCodeError("agent_result_path_invalid", "结果文件路径被目录占用")
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            except OpenCodeError:
                raise
            except OSError as exc:
                raise OpenCodeError("agent_result_file_unreadable", "无法准备 Agent 结果文件路径") from exc

    def create_task_result_paths(self, task_id: str, *, workspace: ResolvedWorkspace | None = None) -> tuple[Path, Path]:
        """兼容调用方名称，创建并返回任务专属结果文件路径。"""
        return self.task_result_paths(task_id, workspace=workspace)

    def _read_result_file(self, result_path: Path, *, workspace: ResolvedWorkspace | None = None) -> dict[str, Any]:
        """读取有限大小的 Agent 结果文件并执行 JSON/schema/业务字段校验。"""
        # 结果读取只允许任务目录内的固定最终文件，避免该通用入口被拿来读取 runtime 外文件。
        result_path = Path(os.path.abspath(Path(result_path).expanduser()))
        result_root = Path(os.path.abspath((workspace.task_results_root.parent if workspace is not None else self.runtime_root / "task-results")))
        try:
            relative = result_path.relative_to(result_root)
        except ValueError as exc:
            raise OpenCodeError("agent_result_path_invalid", "结果文件不在受控任务目录内") from exc
        if len(relative.parts) != 2 or relative.name != RESULT_FILE_NAME:
            raise OpenCodeError("agent_result_path_invalid", "结果文件名称或层级非法")
        limit = int(getattr(self.settings, "agent_result_max_bytes", RESULT_DEFAULT_MAX_BYTES))
        # O_NOFOLLOW 只保护最终节点；父级路径也必须逐级确认不是符号链接。
        self._ensure_no_symlink_path(result_path.parent)
        try:
            info = result_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OpenCodeError("agent_result_file_unreadable", "Agent 结果文件不是普通文件")
            size = info.st_size
        except FileNotFoundError as exc:
            raise OpenCodeError("agent_result_file_missing", "Agent 未生成结果文件") from exc
        except OpenCodeError:
            raise
        except OSError as exc:
            raise OpenCodeError("agent_result_file_unreadable", "Agent 结果文件不可读取") from exc
        if size > limit:
            raise OpenCodeError("agent_result_file_too_large", "Agent 结果文件超过大小限制")
        try:
            descriptor = os.open(result_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                payload = os.read(descriptor, limit + 1)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise OpenCodeError("agent_result_file_unreadable", "Agent 结果文件不可读取") from exc
        if len(payload) > limit:
            raise OpenCodeError("agent_result_file_too_large", "Agent 结果文件超过大小限制")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenCodeError("agent_result_file_invalid_json", "Agent 结果文件不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise OpenCodeError("agent_result_file_schema_invalid", "Agent 结果文件必须是 JSON 对象")
        try:
            return self.validate_candidate(value)
        except OpenCodeError as exc:
            raise OpenCodeError("agent_result_file_schema_invalid", str(exc)) from exc

    def read_result_file(self, result_path: Path, *, workspace: ResolvedWorkspace | None = None) -> dict[str, Any]:
        """读取并校验任务结果文件，供任务处理器和测试直接调用。"""
        return self._read_result_file(result_path, workspace=workspace)

    def cleanup_task_results(self, *, keep_task_id: str | None = None) -> int:
        """按保留天数和最大任务数清理旧产物，不删除当前任务目录。"""
        root = self.runtime_root / "task-results"
        with self._process_lock:
            # 清理逻辑不能让 root 或祖先的符号链接把删除范围带到 runtime 外部。
            try:
                self._ensure_no_symlink_path(root)
                info = root.lstat()
            except (FileNotFoundError, OSError, OpenCodeError):
                return 0
            if not stat.S_ISDIR(info.st_mode):
                return 0
            try:
                entries_info = []
                for item in root.iterdir():
                    try:
                        item_info = item.lstat()
                    except OSError:
                        continue
                    if stat.S_ISDIR(item_info.st_mode) and not stat.S_ISLNK(item_info.st_mode):
                        entries_info.append((item, item_info.st_mtime))
            except OSError:
                return 0
            now = time.time()
            retention = int(getattr(self.settings, "agent_result_retention_days", 14)) * 86400
            active_task_ids = set(self._active_task_ids)
            active_task_ids.update(task_id for task_id in self._process_tasks.values() if task_id)
            protected_task_ids = active_task_ids | ({keep_task_id} if keep_task_id else set())
            entries_info = [(item, mtime) for item, mtime in entries_info if item.name not in protected_task_ids]
            entries_info.sort(key=lambda item: item[1], reverse=True)
            max_tasks = int(getattr(self.settings, "agent_result_max_tasks", 500))
            removed = 0
            for index, (entry, mtime) in enumerate(entries_info):
                if index >= max_tasks or now - mtime > retention:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            return removed

    def _run_command(
        self,
        image: Path,
        prompt: str,
        *,
        slot_id: int | None = None,
        task_id: str | None = None,
        result_path: Path | None = None,
        callback_token: str | None = None,
        resume_session_id: str | None = None,
        workspace: ResolvedWorkspace | None = None,
    ) -> list[str]:
        """构造宿主模式单图研究 CLI 参数；续跑只能显式传入 session。"""
        # host 回滚支持开发者直接检查临时图片；业务 API 在提交前仍通过 map_image_path
        # 验证受控图片根，executor 路径则单独执行同一边界校验。
        image_path = image.expanduser().resolve()
        active_workspace = workspace or ResolvedWorkspace(
            selector="local",
            directory=self.workspace,
            config_file=self.workspace / "opencode.json",
            config_dir=self.workspace / ".opencode",
            images_root=self._configured_image_root(),
            metadata_root=self._configured_image_root(),
            skill_root=self.project_root / "skills" / "research-meme-context",
            task_scratch_root=self.workspace / "tasks" / (task_id or "interactive"),
            task_results_root=self.runtime_root / "task-results" / (task_id or "interactive"),
            db_path=self.db_path,
            local=True,
        )
        executable = str(self.settings.opencode_executable or "opencode")
        command = [
            executable,
            "run",
            # 非交互任务无法回答 OpenCode 的外部目录询问；runtime 边界负责限制可见范围。
            "--auto",
            "--dir",
            str(active_workspace.directory),
            "--format",
            "json",
            "--file",
            str(image_path),
            "--model",
            str(self.settings.opencode_model),
            "--variant",
            OPENCODE_REASONING_VARIANT,
            "--title",
            f"mememeow-task-{task_id or 'interactive'}",
        ]
        if resume_session_id:
            command.extend(("--session", resume_session_id))
        command.append(prompt)
        return command

    def run(
        self,
        image: Path,
        progress: Callable[[float | None, str | None], None],
        *,
        task_id: str | None = None,
        reverse_image_policy: str = "forbid",
        callback_token: str | None = None,
        resume_session_id: str | None = None,
        resume_of_attempt_id: str | None = None,
        processing_config_hash: str | None = None,
        workspace_context: TrustedWorkspaceContext | None = None,
        model_capability: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """执行单张图片研究；失败也尽力返回可验证 session 诊断。"""
        task_id = task_id or uuid.uuid4().hex
        # host 回滚模式也需要 attempt 级诊断标识；executor 模式提交后会以
        # 服务端生成的独立 attempt 覆盖该临时值。
        local_executor_attempt_id = f"host-attempt-{uuid.uuid4().hex}"
        self._remember_executor_attempt(task_id, local_executor_attempt_id)
        if self._take_pre_cancelled(task_id) or self._closing.is_set():
            raise OpenCodeError("task_interrupted", "OpenCode runner 正在关闭或任务已取消", executor_attempt_id=local_executor_attempt_id)
        context = workspace_context or TrustedWorkspaceContext(
            task_id=task_id,
            attempt_id=local_executor_attempt_id,
            scope_id="local",
            selector="local",
            session_id=resume_session_id,
            resume_of_attempt_id=resume_of_attempt_id,
        )
        if workspace_context is not None:
            if workspace_context.task_id != task_id:
                raise OpenCodeError("opencode_workspace_mismatch", "workspace 上下文与任务不一致", executor_attempt_id=local_executor_attempt_id)
            # capability 的 attempt_id 必须与本次 executor 请求使用的独立 attempt
            # 完全相同；scope/selector/session 等业务事实仍来自调用方 claim。
            context = replace(context, task_id=task_id, attempt_id=local_executor_attempt_id)
        if context.task_id != task_id:
            raise OpenCodeError("opencode_workspace_mismatch", "workspace 上下文与任务不一致", executor_attempt_id=local_executor_attempt_id)
        resolved_workspace = self.resolve_workspace(context)
        with self._process_lock:
            # 即使 capability 签发失败，也保留已解析的 opaque selector 供失败诊断和
            # claim fencing 写回；目录与 OpenCode 进程仍不会在此处创建。
            self._workspace_selectors[task_id] = resolved_workspace.selector
            while len(self._workspace_selectors) > 5000:
                self._workspace_selectors.pop(next(iter(self._workspace_selectors)))
        capability = capability_for_provider(self.workspace_provider, context, resolved_workspace)
        if self.executor_mode and not resolved_workspace.local and not capability:
            raise OpenCodeError("opencode_workspace_capability_unavailable", "非 local executor workspace 缺少 capability", executor_attempt_id=local_executor_attempt_id)
        with self._process_lock:
            self._workspace_by_task[task_id] = resolved_workspace
            if capability:
                self._workspace_capabilities[task_id] = capability
        # provider 解析在任何 OpenCode 子进程和结果文件副作用前完成；此时才准备
        # runtime 和当前 workspace 配置。
        try:
            self.prepare_runtime()
            if not self.executor_mode:
                # 外部 provider 的 Task 临时目录和配置只在 capability 已经验证后
                # 创建；解析失败不能留下可写任务目录或结果目录。
                validate_directory_path(resolved_workspace.task_scratch_root, create=True, code="opencode_workspace_invalid")
                validate_directory_path(resolved_workspace.config_dir, create=True, code="opencode_workspace_invalid")
                self._write_workspace_config(resolved_workspace)
            slot_id, lock_handle = self._acquire_slot()
        except Exception:
            with self._process_lock:
                self._workspace_by_task.pop(task_id, None)
                self._workspace_capabilities.pop(task_id, None)
            raise
        try:
            with self._process_lock:
                self._active_task_ids.add(task_id)
                if self.executor_mode:
                    self._active_executor_task_ids.add(task_id)
                _draft_path, result_path = self.task_result_paths(task_id, workspace=resolved_workspace)
            if resume_session_id and not normalize_identifier(resume_session_id, kind="session"):
                raise OpenCodeError("session_binding_mismatch", "续跑 session 标识无效", executor_attempt_id=local_executor_attempt_id)
            # 续跑必须保留既有 draft/中间产物；新业务 attempt 才初始化结果文件。
            if not resume_session_id:
                self._reset_task_result_files(_draft_path, result_path)
            result_runtime_root = self.runtime_root
            active_image = Path(image)
            if context.image_relative_path:
                try:
                    active_image = resolved_workspace.image_path(context.image_relative_path)
                except WorkspaceResolutionError as exc:
                    raise OpenCodeError(exc.code, str(exc), executor_attempt_id=local_executor_attempt_id) from exc
            if self.executor_mode:
                if context.image_relative_path:
                    relative_image = context.image_relative_path.replace("\\", "/")
                else:
                    mapped_image = self.map_image_path(active_image)
                    try:
                        relative_image = mapped_image.relative_to(EXECUTOR_IMAGE_ROOT).as_posix()
                    except ValueError as exc:
                        raise OpenCodeError("agent_image_path_forbidden", "图片路径不在 executor 受控目录内") from exc
                if self._take_pre_cancelled(task_id):
                    raise OpenCodeError("task_interrupted", "Agent 任务已取消", executor_attempt_id=local_executor_attempt_id)
                progress(0.1, "正在提交 Agent executor 任务")
                try:
                    response = self.executor.run(
                        task_id=task_id,
                        image_relative_path=relative_image,
                        reverse_image_policy=reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
                        timeout_seconds=min(int(self.settings.opencode_timeout_seconds), int(getattr(self.settings, "agent_executor_max_timeout_seconds", 1800))),
                        callback_token=callback_token,
                        session_id=resume_session_id,
                        resume_of_attempt_id=resume_of_attempt_id,
                        processing_config_hash=processing_config_hash,
                        executor_attempt_id=local_executor_attempt_id,
                        workspace_selector=None if resolved_workspace.local else resolved_workspace.selector,
                        workspace_capability=capability,
                        model_capability=model_capability,
                    )
                    if response.executor_attempt_id:
                        self._remember_executor_attempt(task_id, response.executor_attempt_id)
                except AgentExecutorError as exc:
                    code = self._executor_error_code(exc.code)
                    if exc.executor_attempt_id:
                        self._remember_executor_attempt(task_id, exc.executor_attempt_id)
                    if code in {"agent_timeout", "agent_executor_unavailable", "agent_runtime_unavailable", "agent_executor_invalid_response"}:
                        try:
                            self.executor.cancel(exc.executor_attempt_id or task_id)
                        except AgentExecutorError:
                            pass
                    decision = classify_resume_error(code, session_id=exc.session_id, target_unchanged=True, grant_state="committed")
                    if code == "agent_timeout":
                        raise OpenCodeError("agent_timeout", "OpenCode 执行超时", session_id=exc.session_id, executor_attempt_id=exc.executor_attempt_id, http_status=exc.http_status) from exc
                    raise OpenCodeError(code, str(exc)[:500], session_id=exc.session_id, executor_attempt_id=exc.executor_attempt_id, retryable=decision.retryable, http_status=exc.http_status) from exc
                if self._take_pre_cancelled(task_id):
                    try:
                        self.executor.cancel(response.executor_attempt_id or task_id)
                    except AgentExecutorError:
                        pass
                    raise OpenCodeError("task_interrupted", "Agent 任务已取消", executor_attempt_id=response.executor_attempt_id or local_executor_attempt_id)
                if response.status != "succeeded":
                    raise OpenCodeError("agent_executor_invalid_response", "Agent executor 任务未返回成功状态", session_id=response.session_id, executor_attempt_id=response.executor_attempt_id)
                if not response.session_id:
                    raise OpenCodeError(
                        "agent_output_invalid_json",
                        "Agent executor 未返回 session 标识",
                        executor_attempt_id=response.executor_attempt_id or local_executor_attempt_id,
                    )
                progress(0.65, "正在读取研究结果文件")
                candidate = self._read_result_file(result_path, workspace=resolved_workspace)
                progress(0.9, "正在校验并写入语境")
                return candidate, response.session_id
            resume_instruction = (
                "这是同一 OpenCode session 的受控续跑；保留并检查已有草稿和中间产物，只完成尚未完成的工作。"
                if resume_session_id
                else "这是首次执行；先建立任务草稿并逐步完成研究。"
            )
            prompt = (
                "使用 research-meme-context skill 分析这张表情包。"
                "遇到错误时自行尝试可行的替代方案；确认无法解决时，简短说明原因并退出。"
                f"本任务 reverse_image_policy={reverse_image_policy if reverse_image_policy in {'forbid', 'auto'} else 'forbid'}；只能通过项目内部反向图片接口使用能力，绝不读取或请求供应商密钥。"
                f"{resume_instruction}"
                f"结果必须写入 {resolved_workspace.result_path}。"
                f"先在同目录写入 {resolved_workspace.draft_path}，"
                "使用 output-schema.json 校验完整 JSON 对象后，使用同一文件系统的原子 rename/mv 将草稿替换为最终文件。"
                "不要把业务 JSON 作为 assistant 文本交付，不要写入数据库；完成后简短说明即可。"
            )
            command = self._run_command(active_image, prompt, slot_id=slot_id, task_id=task_id, result_path=result_path, callback_token=callback_token, resume_session_id=resume_session_id, workspace=resolved_workspace)
            execution_command = command
            environment = self.build_environment(slot_id, task_id, callback_token, workspace=resolved_workspace)
            progress(0.1, f"正在启动语境研究（slot {slot_id}）")
            # 由临时文件承接完整事件流，避免按总字节数拒绝合法的大输出，也避免管道缓存堆积在内存。
            with (
                tempfile.TemporaryFile(mode="w+b", prefix="stdout-", dir=self.log_root) as stdout_stream,
                tempfile.TemporaryFile(mode="w+b", prefix="stderr-", dir=self.log_root) as stderr_stream,
            ):
                process = subprocess.Popen(
                    execution_command,
                    cwd=resolved_workspace.directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    start_new_session=True,
                )
                self._track_process(process, task_id)
                timed_out = False
                interrupted = False
                try:
                    if self._take_pre_cancelled(task_id):
                        self._terminate(process)
                        interrupted = True
                    else:
                        # stdout/stderr 已重定向到临时文件；communicate 只等待进程。
                        process.communicate(timeout=self.settings.opencode_timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    timed_out = True
                finally:
                    self._untrack_process(process)
                interrupted = interrupted or self._take_pre_cancelled(task_id)
                log_path = self.log_root / f"{hashlib.sha256(str(image).encode()).hexdigest()[:16]}.jsonl"
                stdout_stream.flush()
                stderr_stream.flush()
                self._write_diagnostic_log(stdout_stream, log_path)
                session_id = None
                stdout_stream.seek(0)
                for line in stdout_stream:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # 失败路径只要已有合法 session 就继续保留它；成功路径仍由
                        # 结果文件/schema 校验收束，不把噪声日志当成 session。
                        continue
                    found = self._event_session(event)
                    if found:
                        normalized_found = normalize_identifier(found, kind="session")
                        if normalized_found is None:
                            continue
                        if resume_session_id and normalized_found != resume_session_id:
                            raise OpenCodeError("session_binding_mismatch", "OpenCode 返回了不匹配的 session", session_id=normalized_found, executor_attempt_id=local_executor_attempt_id)
                        if session_id and normalized_found != session_id:
                            raise OpenCodeError("session_binding_mismatch", "OpenCode 返回了多个不匹配的 session", session_id=normalized_found, executor_attempt_id=local_executor_attempt_id)
                        session_id = normalized_found
                if not session_id:
                    session_id = resume_session_id
                if interrupted:
                    raise OpenCodeError("task_interrupted", "Agent 任务已取消", session_id=session_id, executor_attempt_id=local_executor_attempt_id)
                if timed_out:
                    raise OpenCodeError("agent_timeout", "OpenCode 执行超时", session_id=session_id, executor_attempt_id=local_executor_attempt_id)
                if process.returncode != 0:
                    diagnostic = self._process_error_diagnostic_stream(
                        stdout_stream,
                        stderr_stream,
                        secrets=(
                            str(getattr(self.settings, "opencode_api_key", "") or ""),
                            str(getattr(self.settings, "agent_executor_token", "") or ""),
                            callback_token or "",
                        ),
                    )
                    failure_code = self._classify_process_failure(stdout_stream, stderr_stream)
                    http_status = self._process_http_status(stdout_stream, stderr_stream)
                    decision = classify_resume_error(failure_code, session_id=session_id, target_unchanged=True, grant_state="committed")
                    raise OpenCodeError(failure_code, diagnostic, session_id=session_id, executor_attempt_id=local_executor_attempt_id, retryable=decision.retryable, http_status=http_status)
            if not session_id:
                raise OpenCodeError("agent_output_invalid_json", "OpenCode 未返回 session 标识", executor_attempt_id=local_executor_attempt_id)
            progress(0.65, "正在读取研究结果文件")
            try:
                candidate = self._read_result_file(result_path, workspace=resolved_workspace)
            except OpenCodeError:
                if resume_session_id:
                    # 续跑必须以受控结果文件为边界；已有 draft/result 损坏时
                    # 不能退回 transcript 解析，否则会绕过副作用和产物完整性判定。
                    raise
                # 显式 host 回滚继续兼容旧 session 结果读取；executor 任务在上方已直接失败。
                try:
                    data = self._session_messages(session_id, environment, task_id, workspace=resolved_workspace)
                except TypeError:
                    # 保持旧测试/扩展覆写三参数 session reader 的兼容。
                    data = self._session_messages(session_id, environment, task_id)
                candidate = self.validate_candidate(self.extract_candidate(self._last_assistant_text(data)))
            progress(0.9, "正在校验并写入语境")
            return candidate, session_id
        finally:
            self.cleanup_task_results(keep_task_id=task_id)
            self._unmark_task_active(task_id)
            with self._process_lock:
                self._active_executor_task_ids.discard(task_id)
                self._cancelled_task_ids.discard(task_id)
                self._workspace_by_task.pop(task_id, None)
                self._workspace_capabilities.pop(task_id, None)
            self._release_slot(slot_id, lock_handle)

    @staticmethod
    def _write_diagnostic_log(source: BinaryIO, target: Path) -> None:
        """保存有限诊断前缀；完整输出仍只存在于本次运行的临时文件。"""
        source.seek(0)
        with target.open("wb") as destination:
            remaining = DIAGNOSTIC_LOG_BYTES
            while remaining > 0:
                chunk = source.read(min(STREAM_COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                destination.write(chunk)
                remaining -= len(chunk)

    def shutdown(self) -> None:
        """终止当前受管理进程，供应用生命周期收束调用。"""
        self._closing.set()
        with self._process_lock:
            self._cancelled_task_ids.update(self._active_task_ids)
            executor_business_ids = list(self._active_executor_task_ids)
        for task_id in executor_business_ids:
            try:
                self.executor.cancel(self.executor.attempt_id_for(task_id) or task_id)
            except AgentExecutorError:
                pass
        with self._process_lock:
            processes = list(self._processes)
            if self._process is not None and self._process not in processes:
                processes.append(self._process)
        for process in processes:
            self._terminate(process)

    def cancel(self, task_id: str) -> None:
        """取消指定研究任务；executor 请求远端取消，host 终止对应本地进程组。"""
        if not task_id:
            return
        with self._process_lock:
            if len(self._cancelled_task_ids) >= 5000:
                for stale_id in list(self._cancelled_task_ids - self._active_task_ids):
                    self._cancelled_task_ids.discard(stale_id)
                    if len(self._cancelled_task_ids) < 4000:
                        break
            self._cancelled_task_ids.add(task_id)
        if self.executor_mode:
            try:
                self.executor.cancel(self.executor.attempt_id_for(task_id) or task_id)
            except AgentExecutorError:
                # 数据库任务已被取消时，executor 可能已经重启或忘记任务；不阻塞 API 收束。
                return
            return
        self._terminate_task(task_id)
