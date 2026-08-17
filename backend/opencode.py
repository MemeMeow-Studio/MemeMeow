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
import shlex
import shutil
import signal
import stat
import socket
import subprocess
import tempfile
import time
import uuid
from io import BytesIO
from queue import Queue
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path
from threading import Event, Lock, RLock, Semaphore
from typing import Any, BinaryIO, Callable, Iterator

from backend.config import Settings
from backend.agent_executor import AgentExecutorClient, AgentExecutorError
from backend.metadata import MemeContext

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - 兼容尚未同步依赖的开发环境
    Draft202012Validator = None
    FormatChecker = None


# 此配置只定义兼容 OpenAI API 的通用 provider，不保存部署地址、密钥或模型选择。
# 模型选择由运行命令的 --model 控制；连接信息由同名进程环境变量在 OpenCode 中展开。
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
                "baseURL": "{env:MEMEMEOW_OPENCODE_BASE_URL}",
                "apiKey": "{env:MEMEMEOW_OPENCODE_API_KEY}",
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
CONTAINER_RUNTIME_ROOT = Path("/runtime")
CONTAINER_IMAGE_ROOT = Path("/images")
CONTAINER_SKILL_ROOT = Path("/skills/research-meme-context")
CONTAINER_NODE_MODULES = Path("/opt/mememeow/node_modules")


class OpenCodeError(RuntimeError):
    """携带稳定错误码的 OpenCode 运行失败。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class OpenCodeRunner:
    """在固定 runtime 中按 slot 受控并行执行 OpenCode，并返回已校验研究结果。"""

    def __init__(self, settings: Settings, project_root: Path | None = None):
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
        self._docker_direct_access: bool | None = None
        self.executor = AgentExecutorClient(
            getattr(settings, "agent_executor_url", None),
            getattr(settings, "agent_executor_token", None),
            timeout=int(getattr(settings, "agent_executor_request_timeout_seconds", 1810)),
        )

    @property
    def executor_mode(self) -> bool:
        """判断当前任务是否通过 Compose 内部 executor 执行。"""
        configured = bool(getattr(self.settings, "agent_executor_url", None))
        return configured and getattr(self.settings, "agent_runtime_mode", "auto") != "host"

    @property
    def docker_mode(self) -> bool:
        """判断是否启用旧版 Docker exec 兼容模式。

        Compose 生产配置始终使用 ``executor_mode``；该分支仅保留旧宿主诊断
        和历史夹具的兼容，不会被新的 API 容器路径调用。
        """
        if self.executor_mode:
            return False
        return self.settings.agent_runtime_mode == "docker" or (
            self.settings.agent_runtime_mode == "auto" and bool(self.settings.agent_container_name)
        )

    @property
    def container_name(self) -> str:
        """返回共享容器名称；Docker 模式缺失名称时以稳定错误阻断任务。"""
        if not self.settings.agent_container_name:
            raise OpenCodeError("agent_runtime_unavailable", "共享 Agent 容器未配置")
        return self.settings.agent_container_name

    def _configured_image_root(self) -> Path:
        """解析设置中的图片根目录，统一相对路径基准以匹配 Compose 挂载。"""
        configured = Path(self.settings.image_root or self.project_root / "data" / "images").expanduser()
        if not configured.is_absolute():
            configured = self.project_root / configured
        return configured.resolve()

    def _docker_image_root_matches_mount(self) -> bool:
        """确认 Docker `/images` 的固定挂载来源与服务图片根目录一致。"""
        return self._configured_image_root() == (self.project_root / "data" / "images").resolve()

    def _docker(self, *arguments: str) -> list[str]:
        """构造不经过 shell 的 Docker 命令，避免任务输入参与命令拼接。"""
        return [self.settings.agent_container_runtime, *arguments]

    def _docker_command_for_execution(self, command: list[str]) -> list[str]:
        """为当前宿主用户选择直接 Docker 或安全的 ``sg docker`` 执行方式。"""
        if self._docker_direct_access is None:
            try:
                probe = subprocess.run(
                    [self.settings.agent_container_runtime, "info"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
                self._docker_direct_access = probe.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                self._docker_direct_access = False
        if self._docker_direct_access:
            return command
        # sg 只接受一个命令字符串；逐参数 quote，避免 prompt 或密钥改变命令结构。
        return ["sg", "docker", "-c", " ".join(shlex.quote(value) for value in command)]

    def _container_exec(self, *arguments: str, environment: dict[str, str] | None = None, workdir: str | None = None) -> list[str]:
        """构造共享容器 exec 命令，仅显式加入允许的运行环境变量。"""
        command = self._docker("exec")
        if workdir:
            command.extend(("--workdir", workdir))
        # 使用 `--env KEY` 从调用方白名单环境继承值，避免 API key 出现在宿主命令行。
        for key in (environment or {}):
            command.extend(("--env", key))
        command.append(self.container_name)
        command.extend(arguments)
        return command

    def _container_ready(self) -> tuple[bool, str]:
        """检查 Docker daemon 和共享容器是否处于运行状态。"""
        try:
            command = self._docker_command_for_execution(self._docker("inspect", "--format", "{{.State.Running}}", self.container_name))
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Docker runtime unavailable: {type(exc).__name__}"
        if result.returncode != 0 or result.stdout.strip().lower() != "true":
            return False, "共享 Agent 容器未运行"
        return True, "ok"

    def _container_exec_probe(self, arguments: list[str]) -> tuple[bool, str]:
        """执行不修改状态的容器工具探针。"""
        try:
            command = self._docker_command_for_execution(self._container_exec(*arguments))
            result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, type(exc).__name__
        return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())[:200]

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

    def _write_runtime_config(self) -> None:
        """原子写入 workspace 共享的无密钥 OpenCode 配置。"""
        target = self.workspace / "opencode.json"
        content = json.dumps(RUNTIME_OPENCODE_CONFIG, ensure_ascii=False, indent=2) + "\n"
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
            if not self.settings.opencode_base_url or not self.settings.opencode_api_key:
                raise OpenCodeError("opencode_not_configured", "未配置 OpenCode 服务地址或密钥")
            if self.executor_mode:
                if not self.executor.configured:
                    raise OpenCodeError("agent_executor_not_configured", "Agent executor 地址或凭据 token 未配置")
                health = self.executor.health()
                if not bool(health.get("ready")):
                    raise OpenCodeError("agent_runtime_unavailable", "Agent executor 健康检查未通过")
            elif not self.docker_mode:
                executable = self.settings.opencode_executable
                if not executable or (not Path(executable).is_file() and shutil.which(executable) is None):
                    raise OpenCodeError("opencode_not_configured", "未找到 OpenCode 可执行文件")
                skills_source = self.project_root / "skills" / "research-meme-context"
                # 项目自己的 OpenCode 插件依赖与前端依赖隔离；环境变量可覆盖这一默认共享目录。
                shared_modules = self.settings.opencode_node_modules or self.project_root / ".opencode" / "node_modules"
                if not skills_source.is_dir() or not shared_modules.is_dir() or not (shared_modules / "@ai-sdk" / "openai").is_dir():
                    raise OpenCodeError("opencode_not_configured", "OpenCode skill、共享 node_modules 或 Responses provider 未预先安装")
            elif self.docker_mode:
                if not self._docker_image_root_matches_mount():
                    raise OpenCodeError("agent_image_root_mismatch", "Docker Agent 图片挂载与图片根配置不一致")
                ready, diagnostic = self._container_ready()
                if not ready:
                    raise OpenCodeError("agent_runtime_unavailable", diagnostic)
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            (self.runtime_root / "home").mkdir(parents=True, exist_ok=True)
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.slots_root.mkdir(parents=True, exist_ok=True)
            self.log_root.mkdir(parents=True, exist_ok=True)
            self._write_runtime_config()
            if self.executor_mode:
                # executor 直接挂载只读 skill 和镜像依赖，runtime volume 不保留符号链接，
                # 这样初始化服务在重启时仍能严格拒绝所有链接节点。
                pass
            elif self.docker_mode:
                # 旧版 Docker exec 兼容模式仍使用容器固定路径，避免暴露宿主绝对路径。
                self._link(self.workspace / ".opencode" / "skills" / "research-meme-context", CONTAINER_SKILL_ROOT)
                self._link(self.workspace / "node_modules", CONTAINER_NODE_MODULES)
            else:
                self._link(self.workspace / ".opencode" / "skills" / "research-meme-context", skills_source)
                self._link(self.workspace / "node_modules", shared_modules)
            self._runtime_ready = True

    def runtime_probe(self) -> dict[str, object]:
        """返回共享 runtime、skill 和依赖探针结果，供启动诊断使用。"""
        if self.executor_mode:
            try:
                health = self.executor.health()
            except AgentExecutorError:
                health = {}
            ready = bool(health.get("ready"))
            runtime_ready = bool(health.get("runtime_read_write"))
            images_ready = bool(health.get("images_read_only"))
            skills_ready = bool(health.get("skills_read_only"))
            executable_ready = bool(health.get("opencode"))
            socket_absent = bool(health.get("docker_socket_absent"))
            return {
                "mode": "compose-agent-executor",
                "container_name": getattr(self.settings, "agent_container_name", None),
                "executor_url_configured": bool(self.executor.url),
                "executor_token_configured": bool(self.executor.token),
                "container_running": ready,
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
                "verified": bool(ready and self.executor.configured and runtime_ready and images_ready and skills_ready and executable_ready and socket_absent),
            }
        if self.docker_mode:
            running, running_detail = self._container_ready()
            image_root_match = self._docker_image_root_matches_mount()
            probes = {
                "opencode": self._container_exec_probe(["opencode", "--version"]) if running else (False, "container_not_running"),
                "runtime_read_write": self._container_exec_probe(["sh", "-lc", "test -r /runtime && test -w /runtime"]) if running else (False, "container_not_running"),
                "images_read_only": self._container_exec_probe(["sh", "-lc", "test -r /images && test ! -w /images"]) if running else (False, "container_not_running"),
                "skill_read_only": self._container_exec_probe(["sh", "-lc", "test -r /skills/research-meme-context && test ! -w /skills/research-meme-context"]) if running else (False, "container_not_running"),
                "dependencies_read_only": self._container_exec_probe(["sh", "-lc", "test -r /opt/mememeow/node_modules && test ! -w /opt/mememeow/node_modules"]) if running else (False, "container_not_running"),
                "non_root": self._container_exec_probe(["sh", "-lc", 'test "$(id -u)" != 0']) if running else (False, "container_not_running"),
                "network": self._container_exec_probe(["sh", "-lc", "curl -fsS --max-time 3 https://example.com >/dev/null"]) if running else (False, "container_not_running"),
                "docker_socket_absent": self._container_exec_probe(["sh", "-lc", "test ! -S /var/run/docker.sock"]) if running else (False, "container_not_running"),
            }
            tool_values = {key: value[0] for key, value in probes.items()}
            return {
                "mode": "shared-docker-container",
                "container_name": self.settings.agent_container_name,
                "container_running": running,
                "container_diagnostic": running_detail,
                "image_root_match": image_root_match,
                "runtime_root_ready": self.runtime_root.is_dir(),
                "workspace_ready": self.workspace.is_dir(),
                "db_path": "opencode.db",
                "executable_ready": tool_values["opencode"],
                "skills_ready": tool_values["skill_read_only"],
                "dependencies_ready": tool_values["opencode"] and tool_values["dependencies_read_only"],
                "mounts_ready": tool_values["runtime_read_write"] and tool_values["images_read_only"] and tool_values["skill_read_only"],
                "non_root": tool_values["non_root"],
                "network_ready": tool_values["network"],
                "docker_socket_absent": tool_values["docker_socket_absent"],
                "tools": tool_values,
                "concurrency": self.concurrency,
                "verified": bool(running and image_root_match and all(tool_values.values()) and self.runtime_root.is_dir()),
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

    def build_environment(self, slot_id: int | None = None, task_id: str | None = None, callback_token: str | None = None) -> dict[str, str]:
        """构造隔离的 OpenCode 进程环境，供后台任务和交互检查入口共同使用。"""
        if self.executor_mode:
            # API 不启动 OpenCode 子进程；该快照仅供诊断，绝不把 executor token
            # 或宿主环境传给 Agent。模型密钥由 executor 自身从 Compose 环境读取。
            values = {
                "OPENCODE_DB": "/runtime/opencode.db",
                "OPENCODE_CONFIG": "/runtime/workspace/opencode.json",
                "OPENCODE_CONFIG_DIR": "/runtime/workspace/.opencode",
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
        # host/dedicated 兼容模式也必须使用白名单；继承整个 API 环境会把 callback
        # 根 secret、executor token、数据库配置或其它 scope 配置带进 Agent 子进程。
        runtime_root = CONTAINER_RUNTIME_ROOT if self.docker_mode else self.runtime_root
        workspace = runtime_root / "workspace"
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            # 使用 runtime 专属 HOME，避免 host 兼容模式读取调用用户的凭据目录。
            "HOME": str(self.runtime_root / "home"),
            "OPENCODE_DB": str(runtime_root / "opencode.db"),
            "OPENCODE_CONFIG": str(workspace / "opencode.json"),
            "OPENCODE_CONFIG_DIR": str(workspace / ".opencode"),
            # 禁止向上合并项目根配置，避免任务意外使用其他 provider 或本地凭据。
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "MEMEMEOW_DATA_ROOT": str(runtime_root),
            "MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT": str(runtime_root / "reverse_image_cache" / "serpapi_google_lens"),
            "MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL": self._agent_reverse_image_url(),
            # 视觉 Skill 只接收 task-scoped 内部地址，不获得数据库或模型运行时权限。
            "MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL": self._agent_visual_search_url(),
        }
        if not self.docker_mode:
            environment["MEMEMEOW_OPENCODE_BASE_URL"] = str(self.settings.opencode_base_url or "")
            environment["MEMEMEOW_OPENCODE_API_KEY"] = str(self.settings.opencode_api_key or "")
            environment["NODE_PATH"] = str(self.workspace / "node_modules")
        if slot_id is not None:
            environment["MEMEMEOW_OPENCODE_SLOT"] = str(slot_id)
        if task_id:
            environment["MEMEMEOW_AGENT_TASK_ID"] = str(task_id)
        if callback_token:
            environment["MEMEMEOW_AGENT_CALLBACK_TOKEN"] = callback_token
        return environment

    def _allowed_container_environment(self, slot_id: int, task_id: str | None = None, callback_token: str | None = None) -> dict[str, str]:
        """返回 Docker exec 白名单环境，禁止继承宿主其他环境变量。"""
        values = {
            "OPENCODE_DB": "/runtime/opencode.db",
            "OPENCODE_CONFIG": "/runtime/workspace/opencode.json",
            "OPENCODE_CONFIG_DIR": "/runtime/workspace/.opencode",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "MEMEMEOW_OPENCODE_BASE_URL": str(self.settings.opencode_base_url or ""),
            "MEMEMEOW_OPENCODE_API_KEY": str(self.settings.opencode_api_key or ""),
            "MEMEMEOW_OPENCODE_SLOT": str(slot_id),
            "MEMEMEOW_AGENT_CONTAINER": self.container_name,
            # Skill 的可选反向图片检索只通过单一密钥和 runtime 缓存目录获得配置。
            "MEMEMEOW_DATA_ROOT": "/runtime",
            "MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT": "/runtime/reverse_image_cache/serpapi_google_lens",
            "MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL": self._agent_reverse_image_url(),
            "MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL": self._agent_visual_search_url(),
        }
        if task_id:
            values["MEMEMEOW_AGENT_TASK_ID"] = task_id
        if callback_token:
            values["MEMEMEOW_AGENT_CALLBACK_TOKEN"] = callback_token
        return values

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
            return self._slot_ids.get(), None
        while not self._closing.is_set():
            slot_id = self._slot_ids.get()
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
    def _process_error_diagnostic(stdout: bytes, stderr: bytes) -> str:
        """从 CLI 的 stderr 或 JSONL error 事件提取有限且可展示的诊断。"""
        return OpenCodeRunner._process_error_diagnostic_stream(BytesIO(stdout), BytesIO(stderr))

    @staticmethod
    def _process_error_diagnostic_stream(stdout: BinaryIO, stderr: BinaryIO) -> str:
        """从临时输出文件提取诊断，读取量受控且不影响完整输出处理。"""
        stderr.seek(0)
        stderr_text = stderr.read(STREAM_COPY_CHUNK_BYTES).decode("utf-8", errors="replace").strip()
        if stderr_text:
            return stderr_text[:500]
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
                diagnostic = f"{message.strip()}{suffix}"
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
        """执行输出 schema 所需字段和 Pydantic 字段约束。"""
        required = {"title", "summary", "subjects", "visible_text", "references", "meaning", "keywords", "search_queries", "uncertainties"}
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
            except OSError:
                pass

    def _terminate_container_session(self, task_id: str | None) -> None:
        """终止指定 task 的容器进程，等待退出并以 KILL 兜底，不停止共享容器。"""
        if not self.docker_mode or not task_id:
            return
        # 每个任务的 OpenCode 命令带唯一 title；先 TERM，再确认匹配进程消失，最后 KILL。
        pattern = f"mememeow-task-{re.escape(task_id)}"
        try:
            for signal_name, wait_seconds in (("TERM", 2.0), ("KILL", 1.0)):
                self._container_signal_processes(pattern, signal_name)
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    if not self._container_has_task_processes(pattern):
                        return
                    time.sleep(0.05)
            # KILL 后再发一次，覆盖进程在探针间隙重新出现的极端情况。
            self._container_signal_processes(pattern, "KILL")
        except (OpenCodeError, OSError, subprocess.TimeoutExpired):
            pass

    def _container_signal_processes(self, pattern: str, signal_name: str) -> None:
        """按唯一任务标题向容器内匹配进程发送指定信号。"""
        command = (
            f"self=$$; for pid in $(pgrep -f -- {shlex.quote(pattern)} || true); do "
            f"[ \"$pid\" = \"$self\" ] || kill -{signal_name} \"$pid\" 2>/dev/null || true; done"
        )
        subprocess.run(
            self._docker_command_for_execution(self._container_exec("sh", "-lc", command)),
            capture_output=True,
            timeout=5,
            check=False,
        )

    def _container_has_task_processes(self, pattern: str) -> bool:
        """探测容器内是否仍有匹配任务进程。"""
        command = (
            f"self=$$; found=1; for pid in $(pgrep -f -- {shlex.quote(pattern)} || true); do "
            f"[ \"$pid\" = \"$self\" ] || found=0; done; exit $found"
        )
        try:
            result = subprocess.run(
                self._docker_command_for_execution(self._container_exec("sh", "-lc", command)),
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        return result.returncode == 0

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

    def _session_messages(self, session_id: str, environment: dict[str, str]) -> object:
        """经临时 loopback server 读取完整 session，规避 export 内联附件的 CLI 截断。"""
        self.log_root.mkdir(parents=True, exist_ok=True)
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
            cwd=self.workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._track_process(server)
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
        if (self.executor_mode or self.docker_mode) and root != (self.project_root / "data" / "images").resolve():
            raise OpenCodeError("agent_image_root_mismatch", "Docker Agent 图片挂载与图片根配置不一致")
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
        return CONTAINER_IMAGE_ROOT / Path(relative.as_posix()) if (self.executor_mode or self.docker_mode) else candidate

    def map_host_image_path(self, image: Path) -> Path:
        """兼容诊断和测试入口，返回图片在当前运行时中的路径。"""
        return self.map_image_path(image)

    def task_result_paths(self, task_id: str) -> tuple[Path, Path]:
        """创建任务专属结果目录并返回草稿、最终临时 JSON 路径。"""
        with self._process_lock:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id or ""):
                raise OpenCodeError("agent_result_path_invalid", "任务结果标识非法")
            root = self.runtime_root / "task-results"
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

    def create_task_result_paths(self, task_id: str) -> tuple[Path, Path]:
        """兼容调用方名称，创建并返回任务专属结果文件路径。"""
        return self.task_result_paths(task_id)

    def _read_result_file(self, result_path: Path) -> dict[str, Any]:
        """读取有限大小的 Agent 结果文件并执行 JSON/schema/业务字段校验。"""
        # 结果读取只允许任务目录内的固定最终文件，避免该通用入口被拿来读取 runtime 外文件。
        result_path = Path(os.path.abspath(Path(result_path).expanduser()))
        result_root = Path(os.path.abspath(self.runtime_root / "task-results"))
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

    def read_result_file(self, result_path: Path) -> dict[str, Any]:
        """读取并校验任务结果文件，供任务处理器和测试直接调用。"""
        return self._read_result_file(result_path)

    def container_command(self, image: Path, prompt: str, *, slot_id: int = 0, task_id: str | None = None) -> list[str]:
        """返回不经 shell 的共享容器 OpenCode 命令，便于启动诊断和参数审计。"""
        return self._run_command(image, prompt, slot_id=slot_id, task_id=task_id)

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

    def _run_command(self, image: Path, prompt: str, *, slot_id: int | None = None, task_id: str | None = None, result_path: Path | None = None, callback_token: str | None = None) -> list[str]:
        """构造单图研究 CLI 参数，固定模型推理变体以保证结果质量一致。"""
        image_path = self.map_image_path(image) if self.docker_mode else image.expanduser().resolve()
        executable = str(self.settings.opencode_executable or "opencode")
        if self.docker_mode and Path(executable).is_absolute() and not executable.startswith(("/runtime/", "/opt/", "/usr/", "/bin/", "/sbin/")):
            # Docker 模式不能把宿主绝对路径交给容器；镜像内命令作为生产默认值。
            executable = "opencode"
        command = [
            executable,
            "run",
            # 非交互任务无法回答 OpenCode 的外部目录询问；容器挂载边界负责限制实际可见范围。
            "--auto",
            "--dir",
            "/runtime/workspace" if self.docker_mode else str(self.workspace),
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
            prompt,
        ]
        if self.docker_mode:
            if slot_id is None:
                slot_id = 0
            environment = self._allowed_container_environment(slot_id, task_id, callback_token)
            return self._container_exec(*command, environment=environment, workdir="/runtime/workspace")
        return command

    def run(self, image: Path, progress: Callable[[float | None, str | None], None], *, task_id: str | None = None, reverse_image_policy: str = "forbid", callback_token: str | None = None) -> tuple[dict[str, Any], str]:
        """执行单张图片研究并从任务专属结果文件接收候选与 session ID。"""
        self.prepare_runtime()
        slot_id, lock_handle = self._acquire_slot()
        task_id = task_id or uuid.uuid4().hex
        try:
            with self._process_lock:
                self._active_task_ids.add(task_id)
                if self.executor_mode:
                    self._active_executor_task_ids.add(task_id)
                _draft_path, result_path = self.task_result_paths(task_id)
            self._reset_task_result_files(_draft_path, result_path)
            result_runtime_root = CONTAINER_RUNTIME_ROOT if self.docker_mode else self.runtime_root
            if self.executor_mode:
                mapped_image = self.map_image_path(image)
                try:
                    relative_image = mapped_image.relative_to(CONTAINER_IMAGE_ROOT).as_posix()
                except ValueError as exc:
                    raise OpenCodeError("agent_image_path_forbidden", "图片路径不在 executor 受控目录内") from exc
                progress(0.1, "正在提交 Agent executor 任务")
                try:
                    response = self.executor.run(
                        task_id=task_id,
                        image_relative_path=relative_image,
                        reverse_image_policy=reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
                        timeout_seconds=min(int(self.settings.opencode_timeout_seconds), int(getattr(self.settings, "agent_executor_max_timeout_seconds", 1800))),
                        callback_token=callback_token,
                    )
                except AgentExecutorError as exc:
                    if exc.code == "agent_timeout":
                        try:
                            self.executor.cancel(task_id)
                        except AgentExecutorError:
                            pass
                        raise OpenCodeError("agent_timeout", "OpenCode 执行超时") from exc
                    if exc.code in {"agent_executor_unavailable", "agent_executor_not_configured", "agent_executor_unauthorized"}:
                        raise OpenCodeError("agent_runtime_unavailable", "Agent executor 暂时不可用") from exc
                    raise OpenCodeError(exc.code, str(exc)) from exc
                if response.status not in {"succeeded", "running", "queued"}:
                    raise OpenCodeError("agent_executor_invalid_response", "Agent executor 任务未返回成功状态")
                progress(0.65, "正在读取研究结果文件")
                candidate = self._read_result_file(result_path)
                progress(0.9, "正在校验并写入语境")
                return candidate, response.session_id or task_id
            prompt = (
                "使用 research-meme-context skill 分析这张表情包。"
                "遇到错误时自行尝试可行的替代方案；确认无法解决时，简短说明原因并退出。"
                f"本任务 reverse_image_policy={reverse_image_policy if reverse_image_policy in {'forbid', 'auto'} else 'forbid'}；只能通过项目内部反向图片接口使用能力，绝不读取或请求供应商密钥。"
                f"结果必须写入 {result_runtime_root / 'task-results' / task_id / RESULT_FILE_NAME}。"
                f"先在同目录写入 {result_runtime_root / 'task-results' / task_id / RESULT_DRAFT_NAME}，"
                "使用 output-schema.json 校验完整 JSON 对象后，使用同一文件系统的原子 rename/mv 将草稿替换为最终文件。"
                "不要把业务 JSON 作为 assistant 文本交付，不要写入数据库；完成后简短说明即可。"
            )
            command = self._run_command(image, prompt, slot_id=slot_id, task_id=task_id, result_path=result_path, callback_token=callback_token)
            execution_command = self._docker_command_for_execution(command) if self.docker_mode else command
            environment = self.build_environment(slot_id, task_id, callback_token) if not self.docker_mode else {
                "PATH": os.environ.get("PATH", ""),
                **self._allowed_container_environment(slot_id, task_id, callback_token),
            }
            progress(0.1, f"正在启动语境研究（slot {slot_id}）")
            # 由临时文件承接完整事件流，避免按总字节数拒绝合法的大输出，也避免管道缓存堆积在内存。
            with (
                tempfile.TemporaryFile(mode="w+b", prefix="stdout-", dir=self.log_root) as stdout_stream,
                tempfile.TemporaryFile(mode="w+b", prefix="stderr-", dir=self.log_root) as stderr_stream,
            ):
                process = subprocess.Popen(
                    execution_command,
                    cwd=self.workspace if not self.docker_mode else self.project_root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    start_new_session=True,
                )
                self._track_process(process, task_id)
                try:
                    # stdout/stderr 已重定向到临时文件；communicate 只等待进程。
                    process.communicate(timeout=self.settings.opencode_timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    self._terminate(process)
                    self._terminate_container_session(task_id)
                    raise OpenCodeError("agent_timeout", "OpenCode 执行超时") from exc
                finally:
                    self._untrack_process(process)
                log_path = self.log_root / f"{hashlib.sha256(str(image).encode()).hexdigest()[:16]}.jsonl"
                stdout_stream.flush()
                stderr_stream.flush()
                self._write_diagnostic_log(stdout_stream, log_path)
                if process.returncode != 0:
                    raise OpenCodeError("agent_process_failed", self._process_error_diagnostic_stream(stdout_stream, stderr_stream))
                session_id = None
                stdout_stream.seek(0)
                for line in stdout_stream:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OpenCodeError("agent_event_invalid", "OpenCode JSONL 事件无效") from exc
                    session_id = self._event_session(event) or session_id
            if not session_id:
                raise OpenCodeError("agent_output_invalid_json", "OpenCode 未返回 session 标识")
            progress(0.65, "正在读取研究结果文件")
            try:
                candidate = self._read_result_file(result_path)
            except OpenCodeError:
                # 仅为旧宿主运行器和历史单元夹具保留会话读取兼容；Docker 模式绝不解析 assistant 文本。
                if self.docker_mode:
                    raise
                data = self._session_messages(session_id, environment)
                candidate = self.validate_candidate(self.extract_candidate(self._last_assistant_text(data)))
            progress(0.9, "正在校验并写入语境")
            return candidate, session_id
        finally:
            self.cleanup_task_results(keep_task_id=task_id)
            self._unmark_task_active(task_id)
            with self._process_lock:
                self._active_executor_task_ids.discard(task_id)
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
            executor_task_ids = list(self._active_executor_task_ids)
        for task_id in executor_task_ids:
            try:
                self.executor.cancel(task_id)
            except AgentExecutorError:
                pass
        with self._process_lock:
            processes = list(self._processes)
            if self._process is not None and self._process not in processes:
                processes.append(self._process)
        for process in processes:
            self._terminate(process)
            task_id = self._process_tasks.get(process)
            self._terminate_container_session(task_id)

    def cancel(self, task_id: str) -> None:
        """取消指定研究任务；Compose 模式请求 executor，宿主兼容模式终止本地进程。"""
        if not task_id:
            return
        if self.executor_mode:
            try:
                self.executor.cancel(task_id)
            except AgentExecutorError:
                # 数据库任务已被取消时，executor 可能已经重启或忘记任务；不阻塞 API 收束。
                return
            return
        self._terminate_container_session(task_id)
