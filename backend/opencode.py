"""OpenCode 图片语境生成运行器。

模块只负责准备固定运行目录、调用公开 CLI 并把不可信输出转为候选 JSON；canonical
sidecar 的读取、指纹复核和写入始终由上层元数据服务完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from backend.config import Settings
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


class OpenCodeError(RuntimeError):
    """携带稳定错误码的 OpenCode 运行失败。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class OpenCodeRunner:
    """在固定 runtime 中串行执行 OpenCode，并返回最后 assistant JSON。"""

    def __init__(self, settings: Settings, project_root: Path | None = None):
        self.settings = settings
        self.project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        self.runtime_root = (settings.opencode_runtime_root or settings.data_root / "opencode").expanduser().resolve()
        self.workspace = self.runtime_root / "workspace"
        self.db_path = self.runtime_root / "opencode.db"
        self.lock_path = self.runtime_root / "worker.lock"
        self.log_root = self.runtime_root / "logs"
        self._run_lock = Lock()
        self._process: subprocess.Popen[bytes] | None = None

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
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, target)

    def prepare_runtime(self) -> None:
        """准备可复用 workspace，不执行任何包管理器或依赖下载。"""
        executable = self.settings.opencode_executable
        if not executable or (not Path(executable).is_file() and shutil.which(executable) is None):
            raise OpenCodeError("opencode_not_configured", "未找到 OpenCode 可执行文件")
        if not self.settings.opencode_model:
            raise OpenCodeError("opencode_not_configured", "未配置 OpenCode 模型")
        if not self.settings.opencode_base_url or not self.settings.opencode_api_key:
            raise OpenCodeError("opencode_not_configured", "未配置 OpenCode 服务地址或密钥")
        skills_source = self.project_root / "skills" / "research-meme-context"
        # 项目自己的 OpenCode 插件依赖与前端依赖隔离；环境变量可覆盖这一默认共享目录。
        shared_modules = self.settings.opencode_node_modules or self.project_root / ".opencode" / "node_modules"
        if not skills_source.is_dir() or not shared_modules.is_dir() or not (shared_modules / "@ai-sdk" / "openai").is_dir():
            raise OpenCodeError("opencode_not_configured", "OpenCode skill、共享 node_modules 或 Responses provider 未预先安装")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._write_runtime_config()
        self._link(self.workspace / ".opencode" / "skills" / "research-meme-context", skills_source)
        self._link(self.workspace / "node_modules", shared_modules)

    def skill_hash(self) -> str:
        """计算 skill 文件内容哈希，用于把运行时契约写入任务记录。"""
        source = self.project_root / "skills" / "research-meme-context"
        digest = hashlib.sha256()
        for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
            digest.update(path.relative_to(source).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def build_environment(self) -> dict[str, str]:
        """构造隔离的 OpenCode 进程环境，供后台任务和交互检查入口共同使用。"""
        environment = dict(os.environ)
        environment["OPENCODE_DB"] = str(self.db_path)
        environment["OPENCODE_CONFIG"] = str(self.workspace / "opencode.json")
        environment["OPENCODE_CONFIG_DIR"] = str(self.workspace / ".opencode")
        # 禁止向上合并项目根配置，避免任务意外使用其他 provider 或本地凭据。
        environment["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        environment["MEMEMEOW_OPENCODE_BASE_URL"] = str(self.settings.opencode_base_url)
        environment["MEMEMEOW_OPENCODE_API_KEY"] = str(self.settings.opencode_api_key)
        node_path = str(self.workspace / "node_modules")
        if inherited := environment.get("NODE_PATH"):
            node_path = os.pathsep.join((node_path, inherited))
        environment["NODE_PATH"] = node_path
        return environment

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
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            return stderr_text[:500]
        for line in reversed(stdout.splitlines()):
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
                return f"{message.strip()}{suffix}"[:500]
        return "OpenCode 进程以非零状态退出"

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

    def _session_messages(self, session_id: str, environment: dict[str, str]) -> object:
        """经临时 loopback server 读取完整 session，规避 export 内联附件的 CLI 截断。"""
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
        endpoint = f"http://127.0.0.1:{port}/session/{session_id}/message"
        deadline = time.monotonic() + min(15, self.settings.opencode_timeout_seconds)
        try:
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise OpenCodeError("agent_export_failed", "OpenCode session server 未能启动")
                try:
                    with urlopen(endpoint, timeout=1) as response:  # noqa: S310 - endpoint 固定为本机 loopback。
                        raw = response.read(self.settings.opencode_max_output_bytes + 1)
                    if len(raw) > self.settings.opencode_max_output_bytes:
                        raise OpenCodeError("agent_output_limit", "OpenCode session 消息超过上限")
                    return json.loads(raw)
                except OpenCodeError:
                    raise
                except (OSError, TimeoutError, URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            raise OpenCodeError("agent_export_failed", "无法读取 OpenCode session 消息")
        finally:
            self._terminate(server)

    def _run_command(self, image: Path, prompt: str) -> list[str]:
        """构造单图研究 CLI 参数，固定模型推理变体以保证结果质量一致。"""
        return [
            str(self.settings.opencode_executable),
            "run",
            "--dir",
            str(self.workspace),
            "--format",
            "json",
            "--file",
            str(image.resolve()),
            "--model",
            str(self.settings.opencode_model),
            "--variant",
            OPENCODE_REASONING_VARIANT,
            prompt,
        ]

    def run(self, image: Path, progress: Callable[[float | None, str | None], None]) -> tuple[dict[str, Any], str]:
        """处理单张图片并返回已校验候选与独立 OpenCode session ID。"""
        self.prepare_runtime()
        with self._run_lock:
            # 文件锁还覆盖多应用进程；非 POSIX 环境用进程内锁保持退化可用。
            lock_handle = self.lock_path.open("a+")
            try:
                try:
                    import fcntl

                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
                prompt = "使用 research-meme-context skill 分析这张表情包。只在最后一条 assistant 消息输出符合 output-schema.json 的一个 JSON 对象，不要解释，也不要写入任何文件。"
                command = self._run_command(image, prompt)
                environment = self.build_environment()
                progress(0.1, "正在启动语境研究")
                process = subprocess.Popen(command, cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                self._process = process
                try:
                    stdout, stderr = process.communicate(timeout=self.settings.opencode_timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    self._terminate(process)
                    raise OpenCodeError("agent_timeout", "OpenCode 执行超时") from exc
                finally:
                    self._process = None
                if len(stdout) + len(stderr) > self.settings.opencode_max_output_bytes:
                    raise OpenCodeError("agent_output_limit", "OpenCode 输出超过上限")
                log_path = self.log_root / f"{hashlib.sha256(str(image).encode()).hexdigest()[:16]}.jsonl"
                log_path.write_bytes(stdout[: self.settings.opencode_max_output_bytes])
                if process.returncode != 0:
                    raise OpenCodeError("agent_process_failed", self._process_error_diagnostic(stdout, stderr))
                session_id = None
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OpenCodeError("agent_event_invalid", "OpenCode JSONL 事件无效") from exc
                    session_id = self._event_session(event) or session_id
                if not session_id:
                    raise OpenCodeError("agent_output_invalid_json", "OpenCode 未返回 session 标识")
                progress(0.65, "正在读取研究结果")
                data = self._session_messages(session_id, environment)
                candidate = self.validate_candidate(self.extract_candidate(self._last_assistant_text(data)))
                progress(0.9, "正在校验并写入语境")
                return candidate, session_id
            finally:
                try:
                    import fcntl

                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                lock_handle.close()

    def shutdown(self) -> None:
        """终止当前受管理进程，供应用生命周期收束调用。"""
        process = self._process
        if process is not None:
            self._terminate(process)
