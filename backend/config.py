"""服务端类型化配置与受保护的 dotenv 持久化工具。

该模块位于 API 生命周期与各后端服务之间，统一处理进程环境、`.env` 和默认值。
敏感字段只在服务端内存中保存；设置页面只能通过原子更新修改 Agent 并发数量。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONCURRENCY_ENV = "MEMEMEOW_OPENCODE_CONCURRENCY"
SETTINGS_TOKEN_ENV = "MEMEMEOW_SETTINGS_ADMIN_TOKEN"


class Settings(BaseSettings):
    """FastAPI 进程级配置；启动后保持不变，避免任务执行期间热切换资源。"""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        validate_default=True,
        enable_decoding=False,
    )

    data_root: Path = Field(default_factory=lambda: PROJECT_ROOT / "data", validation_alias=AliasChoices("MEMEMEOW_DATA_ROOT", "data_root"))
    image_root: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_IMAGE_ROOT", "image_root"))
    embedding_api_key: str | None = Field(default=None, validation_alias=AliasChoices("EMBEDDING_API_KEY", "embedding_api_key"), repr=False)
    embedding_base_url: str | None = Field(default=None, validation_alias=AliasChoices("EMBEDDING_BASE_URL", "embedding_base_url"))
    embedding_model: str = Field(default="BAAI/bge-m3", validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model"))
    llm_enhance_model: str | None = Field(default=None, validation_alias=AliasChoices("LLM_ENHANCE_MODEL", "llm_enhance_model"))
    protected_mode: bool = Field(default=False, validation_alias=AliasChoices("MEMEMEOW_PROTECTED_MODE", "protected_mode"))
    allowed_endpoints: tuple[str, ...] = Field(default=("/", "/health", "/search", "/config", "/tasks"), validation_alias=AliasChoices("MEMEMEOW_ALLOWED_ENDPOINTS", "allowed_endpoints"))
    rate_limit_enabled: bool = Field(default=False, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_ENABLED", "rate_limit_enabled"))
    rate_limit_requests: int = Field(default=60, ge=1, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_REQUESTS", "rate_limit_requests"))
    rate_limit_window: int = Field(default=60, ge=1, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_WINDOW", "rate_limit_window"))
    max_upload_size: int = Field(default=20 * 1024 * 1024, ge=1, validation_alias=AliasChoices("MEMEMEOW_MAX_UPLOAD_SIZE", "max_upload_size"))
    opencode_executable: str | None = Field(default="opencode", validation_alias=AliasChoices("MEMEMEOW_OPENCODE_EXECUTABLE", "opencode_executable"))
    opencode_model: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_MODEL", "opencode_model"))
    opencode_base_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_BASE_URL", "opencode_base_url"))
    opencode_api_key: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_API_KEY", "opencode_api_key"), repr=False)
    opencode_runtime_root: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_RUNTIME_ROOT", "opencode_runtime_root"))
    opencode_timeout_seconds: int = Field(default=300, ge=1, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_TIMEOUT_SECONDS", "opencode_timeout_seconds"))
    # 保留旧配置字段供部署和外部调用方读取；OpenCode 输出现在通过临时文件流式承接，不再按该值截断。
    opencode_max_output_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_MAX_OUTPUT_BYTES", "opencode_max_output_bytes"))
    opencode_node_modules: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_NODE_MODULES", "opencode_node_modules"))
    opencode_concurrency: int = Field(default=1, ge=1, le=8, validation_alias=AliasChoices(CONCURRENCY_ENV, "opencode_concurrency"))
    agent_backpressure: int = Field(default=32, ge=1, le=500, validation_alias=AliasChoices("MEMEMEOW_AGENT_BACKPRESSURE", "agent_backpressure"))
    settings_admin_token: str | None = Field(default=None, validation_alias=AliasChoices(SETTINGS_TOKEN_ENV, "settings_admin_token"), repr=False)

    _dotenv_path: Path | None = PrivateAttr(default=None)

    @field_validator("allowed_endpoints", mode="before")
    @classmethod
    def parse_allowed_endpoints(cls, value: Any) -> tuple[str, ...]:
        """兼容旧版逗号分隔白名单，同时清理空项。"""
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (tuple, list, set)):
            raise ValueError("allowed_endpoints_invalid")
        return tuple(str(item).strip() for item in value if str(item).strip())

    @field_validator(
        "image_root",
        "embedding_api_key",
        "embedding_base_url",
        "llm_enhance_model",
        "opencode_executable",
        "opencode_model",
        "opencode_base_url",
        "opencode_api_key",
        "opencode_runtime_root",
        "opencode_node_modules",
        "settings_admin_token",
        mode="before",
    )
    @classmethod
    def empty_optional_values(cls, value: Any) -> Any:
        """保持旧解析行为，将 dotenv 中的空可选字段视为未配置。"""
        return None if value == "" else value

    @model_validator(mode="after")
    def derive_paths(self) -> "Settings":
        """根据 data root 推导未显式配置的图片和 runtime 目录。"""
        if self.image_root is None:
            self.image_root = self.data_root / "images"
        if self.opencode_runtime_root is None:
            self.opencode_runtime_root = self.data_root / "opencode"
        return self

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        """读取 `.env` 和进程环境；进程环境由 Pydantic Settings 自动优先。"""
        path = Path(env_file).expanduser() if env_file else None
        # `_env_file` 是 Pydantic Settings 的启动期覆盖参数，不会修改全局 os.environ。
        settings = cls(_env_file=path if path and path.is_file() else None)
        settings._dotenv_path = path.resolve() if path else None
        return settings

    @property
    def dotenv_path(self) -> Path:
        """返回设置页允许原子更新的 dotenv 路径。"""
        return self._dotenv_path or (PROJECT_ROOT / ".env").resolve()

    @property
    def environment_overrides(self) -> tuple[str, ...]:
        """返回直接覆盖并发设置的进程环境字段。"""
        return (CONCURRENCY_ENV,) if CONCURRENCY_ENV in os.environ else ()

    @property
    def pending_concurrency(self) -> int | None:
        """读取 dotenv 中待重启生效的并发值，不把密钥或其他内容返回。"""
        try:
            values = dotenv_values(self.dotenv_path)
            raw = values.get(CONCURRENCY_ENV)
            if raw in (None, ""):
                return None
            return int(str(raw).strip())
        except (OSError, TypeError, ValueError):
            return None

    @property
    def settings_version(self) -> str:
        """计算只包含非敏感有效配置的稳定版本摘要。"""
        payload = {
            "embedding_model": self.embedding_model,
            "opencode_model": self.opencode_model,
            "opencode_concurrency": self.opencode_concurrency,
            "agent_backpressure": self.agent_backpressure,
            "protected_mode": self.protected_mode,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def ensure_directories(self) -> None:
        """创建受控数据目录，应用启动时调用。"""
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.image_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _configured(value: object) -> bool:
        """判断敏感部署字段是否已配置，仅返回布尔状态。"""
        return bool(value)

    def status(self) -> dict[str, object]:
        """返回兼容 `/config` 的脱敏运行状态，不返回秘密、路径或完整 URL。"""
        return {
            "embedding_model": self.embedding_model,
            "embedding_base_url": "已配置" if self.embedding_base_url else None,
            "embedding_api_key_configured": self._configured(self.embedding_api_key),
            "llm_enhance_model": self.llm_enhance_model,
            "opencode_model": self.opencode_model,
            "opencode_base_url": "已配置" if self.opencode_base_url else None,
            "opencode_api_key_configured": self._configured(self.opencode_api_key),
            "opencode_configured": bool(self.opencode_executable and self.opencode_model and self.opencode_base_url and self.opencode_api_key),
            "data_root_configured": True,
            "opencode_concurrency": self.opencode_concurrency,
            "agent_backpressure": self.agent_backpressure,
            "settings_version": self.settings_version,
        }

    def backend_status(self, *, cache_ready: bool = False, runtime_ready: bool = False) -> dict[str, object]:
        """生成后端设置页三类字段、待生效值和重启提示。"""
        pending = self.pending_concurrency
        environment_override = bool(self.environment_overrides)
        restart_required = pending is not None and pending != self.opencode_concurrency and not environment_override
        readonly = {
            "embedding_model": self.embedding_model,
            "opencode_model": self.opencode_model,
            "opencode_configured": bool(self.opencode_executable and self.opencode_model and self.opencode_base_url and self.opencode_api_key),
            "embedding_cache_ready": cache_ready,
            "runtime_ready": runtime_ready,
            "settings_admin_enabled": bool(self.settings_admin_token),
        }
        editable = {
            "opencode_concurrency": {
                "value": self.opencode_concurrency,
                "pending_value": pending,
                "minimum": 1,
                "maximum": 8,
                "environment_overridden": environment_override,
                "restart_required": restart_required,
            },
        }
        deployment = {
            "opencode_executable": {"configured": bool(self.opencode_executable)},
            "runtime_root": {"configured": bool(self.opencode_runtime_root)},
            "data_root": {"configured": bool(self.data_root)},
            "provider_url": {"configured": bool(self.opencode_base_url or self.embedding_base_url)},
            "api_key": {"configured": bool(self.opencode_api_key or self.embedding_api_key)},
            "protected_mode": self.protected_mode,
        }
        return {
            "settings_version": self.settings_version,
            "config_version": self.settings_version,
            "restart_required": restart_required,
            "effective": {"opencode_concurrency": self.opencode_concurrency},
            "pending": {"opencode_concurrency": pending},
            "effective_value": self.opencode_concurrency,
            "pending_value": pending,
            "environment_overrides": list(self.environment_overrides),
            "readonly": readonly,
            "read_only": readonly,
            "editable": editable,
            "safe_adjustable": editable,
            "deployment": deployment,
            "deployment_only": deployment,
        }


def update_dotenv_concurrency(path: str | Path, value: int) -> Path:
    """原子更新 dotenv 的并发字段，保留未知变量、注释和其他格式。"""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ValueError("opencode_concurrency_out_of_range")
    target = Path(path).expanduser()
    if target.is_symlink():
        raise ValueError("dotenv_symlink_forbidden")
    for parent in (target.parent, *target.parent.parents):
        if parent == parent.parent:
            break
        if parent.is_symlink():
            raise ValueError("dotenv_parent_symlink_forbidden")
    if target.exists() and not target.is_file():
        raise ValueError("dotenv_not_file")
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    lines = original.splitlines(keepends=True)
    replaced = False
    output: list[str] = []
    for line in lines:
        if re.match(rf"^\s*{re.escape(CONCURRENCY_ENV)}\s*=", line):
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body = line[: -len(newline)] if newline else line
            suffix_match = re.search(r"(\s+#.*)$", body)
            suffix = suffix_match.group(1) if suffix_match else ""
            output.append(f"{CONCURRENCY_ENV}={value}{suffix}{newline}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        if output and not output[-1].endswith("\n"):
            output.append("\n")
        output.append(f"{CONCURRENCY_ENV}={value}\n")
    content = "".join(output)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    # 配置只允许当前用户读写；即使旧文件只有单一权限位，也补齐 owner 的读写位。
    mode = (mode & 0o600) | 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target
