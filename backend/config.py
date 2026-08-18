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

from backend.visual_models import ACTIVE_VISUAL_MODEL_ID, active_visual_model_spec, source_repository_valid, visual_model_spec
from executor.token import ExecutorTokenError, read_token_file
from backend.storage_security import StorageRootError, validate_controlled_root


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONCURRENCY_ENV = "MEMEMEOW_OPENCODE_CONCURRENCY"
SETTINGS_TOKEN_ENV = "MEMEMEOW_SETTINGS_ADMIN_TOKEN"
_ACTIVE_VISUAL_SPEC = active_visual_model_spec()


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
    # 当前视觉空间固定为 DINOv2 ViT-B/14；源码和权重均不随主后端镜像分发。
    visual_model: str = Field(default=ACTIVE_VISUAL_MODEL_ID, validation_alias=AliasChoices("MEMEMEOW_VISUAL_MODEL", "VISUAL_MODEL", "visual_model"))
    visual_model_dimensions: int = Field(default=_ACTIVE_VISUAL_SPEC.dimensions, ge=1, le=8192, validation_alias=AliasChoices("MEMEMEOW_VISUAL_DIMENSIONS", "MEMEMEOW_VISUAL_EMBEDDING_DIMENSIONS", "VISUAL_MODEL_DIMENSIONS", "visual_model_dimensions"))
    visual_preprocess_version: str = Field(default=_ACTIVE_VISUAL_SPEC.preprocess_version, min_length=1, max_length=128, validation_alias=AliasChoices("MEMEMEOW_VISUAL_PREPROCESS_VERSION", "VISUAL_PREPROCESS_VERSION", "visual_preprocess_version"))
    visual_weights_path: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_VISUAL_WEIGHTS_PATH", "VISUAL_WEIGHTS_PATH", "visual_weights_path"))
    visual_weights_sha256: str | None = Field(default=None, min_length=64, max_length=64, validation_alias=AliasChoices("MEMEMEOW_VISUAL_WEIGHTS_SHA256", "VISUAL_WEIGHTS_SHA256", "visual_weights_sha256"))
    visual_internal_url: str = Field(default="http://127.0.0.1:8276/internal/visual-embedding", validation_alias=AliasChoices("MEMEMEOW_VISUAL_INTERNAL_URL", "VISUAL_INTERNAL_URL", "visual_internal_url"))
    visual_health_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_VISUAL_HEALTH_URL", "VISUAL_HEALTH_URL", "visual_health_url"))
    visual_search_internal_url: str = Field(default="http://127.0.0.1:8275/internal/visual-search/match", validation_alias=AliasChoices("MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL", "VISUAL_SEARCH_INTERNAL_URL", "visual_search_internal_url"))
    # Agent 容器内的回调地址与后端自身地址分离，Compose 模式使用服务 DNS。
    agent_reverse_image_internal_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_REVERSE_IMAGE_INTERNAL_URL", "agent_reverse_image_internal_url"))
    agent_visual_search_internal_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_VISUAL_SEARCH_INTERNAL_URL", "agent_visual_search_internal_url"))
    # API 只通过 Compose 内网 executor URL 调用 Agent；不会启动 Docker CLI。
    agent_executor_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_EXECUTOR_URL", "agent_executor_url"))
    agent_executor_token: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_EXECUTOR_TOKEN", "agent_executor_token"), repr=False)
    agent_executor_token_file: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_EXECUTOR_TOKEN_FILE", "agent_executor_token_file"), repr=False)
    agent_executor_request_timeout_seconds: int = Field(default=1810, ge=1, le=7200, validation_alias=AliasChoices("MEMEMEOW_AGENT_EXECUTOR_REQUEST_TIMEOUT_SECONDS", "agent_executor_request_timeout_seconds"))
    agent_executor_max_timeout_seconds: int = Field(default=1800, ge=1, le=7200, validation_alias=AliasChoices("MEMEMEOW_AGENT_EXECUTOR_MAX_TIMEOUT_SECONDS", "agent_executor_max_timeout_seconds"))
    visual_internal_token: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_VISUAL_INTERNAL_TOKEN", "VISUAL_INTERNAL_TOKEN", "visual_internal_token"), repr=False)
    agent_callback_secret: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_CALLBACK_SECRET", "agent_callback_secret"), repr=False)
    agent_callback_verification_keys: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_AGENT_CALLBACK_VERIFICATION_KEYS", "agent_callback_verification_keys"), repr=False)
    visual_model_repo: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_VISUAL_MODEL_REPO", "VISUAL_MODEL_REPO", "visual_model_repo"))
    visual_cpu_threads: int = Field(default=4, ge=1, le=128, validation_alias=AliasChoices("MEMEMEOW_VISUAL_CPU_THREADS", "VISUAL_CPU_THREADS", "visual_cpu_threads"))
    visual_cpu_interop_threads: int = Field(default=1, ge=1, le=32, validation_alias=AliasChoices("MEMEMEOW_VISUAL_CPU_INTEROP_THREADS", "VISUAL_CPU_INTEROP_THREADS", "visual_cpu_interop_threads"))
    visual_concurrency: int = Field(default=1, ge=1, le=8, validation_alias=AliasChoices("MEMEMEOW_VISUAL_CONCURRENCY", "VISUAL_CONCURRENCY", "visual_concurrency"))
    visual_max_pixels: int = Field(default=25_000_000, ge=1, le=100_000_000, validation_alias=AliasChoices("MEMEMEOW_VISUAL_MAX_PIXELS", "VISUAL_MAX_PIXELS", "visual_max_pixels"))
    visual_request_timeout_seconds: int = Field(default=120, ge=1, le=3600, validation_alias=AliasChoices("MEMEMEOW_VISUAL_REQUEST_TIMEOUT_SECONDS", "VISUAL_REQUEST_TIMEOUT_SECONDS", "visual_request_timeout_seconds"))
    llm_enhance_model: str | None = Field(default=None, validation_alias=AliasChoices("LLM_ENHANCE_MODEL", "llm_enhance_model"))
    protected_mode: bool = Field(default=False, validation_alias=AliasChoices("MEMEMEOW_PROTECTED_MODE", "protected_mode"))
    allowed_endpoints: tuple[str, ...] = Field(default=("/", "/health", "/search", "/config", "/tasks"), validation_alias=AliasChoices("MEMEMEOW_ALLOWED_ENDPOINTS", "allowed_endpoints"))
    rate_limit_enabled: bool = Field(default=False, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_ENABLED", "rate_limit_enabled"))
    rate_limit_requests: int = Field(default=60, ge=1, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_REQUESTS", "rate_limit_requests"))
    rate_limit_window: int = Field(default=60, ge=1, validation_alias=AliasChoices("MEMEMEOW_RATE_LIMIT_WINDOW", "rate_limit_window"))
    max_upload_size: int = Field(default=20 * 1024 * 1024, ge=1, validation_alias=AliasChoices("MEMEMEOW_MAX_UPLOAD_SIZE", "max_upload_size"))
    opencode_executable: str | None = Field(default="opencode", validation_alias=AliasChoices("MEMEMEOW_OPENCODE_EXECUTABLE", "opencode_executable"))
    agent_runtime_mode: str = Field(default="auto", validation_alias=AliasChoices("MEMEMEOW_AGENT_RUNTIME_MODE", "agent_runtime_mode"))
    opencode_model: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_MODEL", "opencode_model"))
    opencode_base_url: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_BASE_URL", "opencode_base_url"))
    opencode_api_key: str | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_API_KEY", "opencode_api_key"), repr=False)
    serpapi_api_key: str | None = Field(default=None, validation_alias=AliasChoices("SERPAPI_API_KEY", "serpapi_api_key"), repr=False)
    reverse_image_cache_root: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_REVERSE_IMAGE_CACHE_ROOT", "reverse_image_cache_root"))
    reverse_image_internal_url: str = Field(default="http://127.0.0.1:8275/internal/reverse-image/search", validation_alias=AliasChoices("MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL", "reverse_image_internal_url"))
    opencode_runtime_root: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_RUNTIME_ROOT", "opencode_runtime_root"))
    opencode_timeout_seconds: int = Field(default=300, ge=1, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_TIMEOUT_SECONDS", "opencode_timeout_seconds"))
    # 保留旧配置字段供部署和外部调用方读取；OpenCode 输出现在通过临时文件流式承接，不再按该值截断。
    opencode_max_output_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_MAX_OUTPUT_BYTES", "opencode_max_output_bytes"))
    opencode_node_modules: Path | None = Field(default=None, validation_alias=AliasChoices("MEMEMEOW_OPENCODE_NODE_MODULES", "opencode_node_modules"))
    agent_result_max_bytes: int = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024, validation_alias=AliasChoices("MEMEMEOW_AGENT_RESULT_MAX_BYTES", "agent_result_max_bytes"))
    agent_result_retention_days: int = Field(default=14, ge=1, le=365, validation_alias=AliasChoices("MEMEMEOW_AGENT_RESULT_RETENTION_DAYS", "agent_result_retention_days"))
    agent_result_max_tasks: int = Field(default=500, ge=1, le=10000, validation_alias=AliasChoices("MEMEMEOW_AGENT_RESULT_MAX_TASKS", "agent_result_max_tasks"))
    opencode_concurrency: int = Field(default=1, ge=1, le=8, validation_alias=AliasChoices(CONCURRENCY_ENV, "opencode_concurrency"))
    agent_backpressure: int = Field(default=32, ge=1, le=500, validation_alias=AliasChoices("MEMEMEOW_AGENT_BACKPRESSURE", "agent_backpressure"))
    database_url: str = Field(default="postgresql+psycopg://mememeow:mememeow@127.0.0.1:5434/mememeow", validation_alias=AliasChoices("MEMEMEOW_DATABASE_URL", "database_url"), repr=False)
    database_pool_size: int = Field(default=5, ge=1, le=100, validation_alias=AliasChoices("MEMEMEOW_DATABASE_POOL_SIZE", "database_pool_size"))
    database_max_overflow: int = Field(default=10, ge=0, le=100, validation_alias=AliasChoices("MEMEMEOW_DATABASE_MAX_OVERFLOW", "database_max_overflow"))
    database_pool_timeout: int = Field(default=10, ge=1, le=300, validation_alias=AliasChoices("MEMEMEOW_DATABASE_POOL_TIMEOUT", "database_pool_timeout"))
    database_pool_recycle: int = Field(default=1800, ge=60, le=86400, validation_alias=AliasChoices("MEMEMEOW_DATABASE_POOL_RECYCLE", "database_pool_recycle"))
    embedding_dimensions: int = Field(default=1024, validation_alias=AliasChoices("MEMEMEOW_EMBEDDING_DIMENSIONS", "embedding_dimensions"))
    worker_lease_seconds: int = Field(default=120, ge=10, le=3600, validation_alias=AliasChoices("MEMEMEOW_WORKER_LEASE_SECONDS", "worker_lease_seconds"))
    worker_heartbeat_seconds: int = Field(default=30, ge=1, le=120, validation_alias=AliasChoices("MEMEMEOW_WORKER_HEARTBEAT_SECONDS", "worker_heartbeat_seconds"))
    worker_max_attempts: int = Field(default=3, ge=1, le=20, validation_alias=AliasChoices("MEMEMEOW_WORKER_MAX_ATTEMPTS", "worker_max_attempts"))
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
        "visual_weights_path",
        "visual_weights_sha256",
        "visual_internal_token",
        "visual_health_url",
        "visual_model_repo",
        "agent_reverse_image_internal_url",
        "agent_visual_search_internal_url",
        "agent_executor_url",
        "agent_executor_token",
        "agent_executor_token_file",
        "llm_enhance_model",
        "opencode_executable",
        "agent_runtime_mode",
        "opencode_model",
        "opencode_base_url",
        "opencode_api_key",
        "serpapi_api_key",
        "opencode_runtime_root",
        "reverse_image_cache_root",
        "opencode_node_modules",
        "settings_admin_token",
        "agent_callback_verification_keys",
        "database_url",
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
        if self.reverse_image_cache_root is None:
            self.reverse_image_cache_root = self.data_root / "reverse_image_cache" / "serpapi_google_lens"
        if self.embedding_dimensions != 1024:
            raise ValueError("embedding_dimensions_must_be_1024")
        spec = visual_model_spec(self.visual_model)
        if spec is not None:
            if not spec.runtime_supported:
                raise ValueError("visual_model_migration_required")
            if self.visual_model_dimensions != spec.dimensions:
                raise ValueError(f"visual_dimensions_must_be_{spec.dimensions}")
            if self.visual_preprocess_version != spec.preprocess_version:
                raise ValueError("visual_preprocess_version_mismatch")
        if self.visual_weights_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", self.visual_weights_sha256):
            raise ValueError("visual_weights_sha256_invalid")
        if not self.database_url.startswith("postgresql"):
            raise ValueError("postgresql_required")
        if self.agent_runtime_mode not in {"auto", "executor", "host"}:
            raise ValueError("agent_runtime_mode_invalid")
        return self

    @property
    def executor_configured(self) -> bool:
        """返回 executor 地址和 token 是否同时可用，供运行模式选择调用。"""
        return bool(str(self.agent_executor_url or "").strip() and str(self.agent_executor_token or "").strip())

    @property
    def selected_agent_runtime_mode(self) -> str:
        """解析最终执行模式；auto 只有在 executor 凭据完整时才选择 executor。"""
        if self.agent_runtime_mode == "executor":
            return "executor"
        if self.agent_runtime_mode == "auto" and self.executor_configured:
            return "executor"
        return "host"

    @property
    def expected_database_revision(self) -> str:
        """返回公共核心固定要求的 schema revision。"""
        from backend.database import CURRENT_SCHEMA_REVISION

        return CURRENT_SCHEMA_REVISION

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        """读取 `.env` 和进程环境，并在 Compose 模式加载共享 token 文件。

        进程环境由 Pydantic Settings 自动优先；当配置了 token 文件时，文件是
        Compose executor 的唯一凭据来源，避免 `.env` 中的旧 token 与 named
        volume 中的持久凭据不一致。
        """
        path = Path(env_file).expanduser() if env_file else None
        # `_env_file` 是 Pydantic Settings 的启动期覆盖参数，不会修改全局 os.environ。
        settings = cls(_env_file=path if path and path.is_file() else None)
        if settings.agent_executor_token_file is not None:
            try:
                settings.agent_executor_token = read_token_file(settings.agent_executor_token_file)
            except ExecutorTokenError as exc:
                raise ValueError(str(exc)) from exc
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
            "visual_model": self.visual_model,
            "visual_model_dimensions": self.visual_model_dimensions,
            "visual_preprocess_version": self.visual_preprocess_version,
            "opencode_model": self.opencode_model,
            "agent_runtime_mode": self.agent_runtime_mode,
            "agent_executor_configured": bool(self.agent_executor_url),
            "agent_executor_token_configured": bool(self.agent_executor_token),
            "opencode_concurrency": self.opencode_concurrency,
            "agent_backpressure": self.agent_backpressure,
            "protected_mode": self.protected_mode,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def ensure_directories(self) -> None:
        """创建并校验数据、图片、runtime 和缓存目录，应用启动时调用。"""
        # 业务进程创建的 sidecar、缓存和 runtime 文件默认只允许运行身份访问。
        os.umask(0o077)
        roots = (
            ("data_root", self.data_root),
            ("image_root", self.image_root),
            ("opencode_runtime_root", self.opencode_runtime_root),
            ("reverse_image_cache_root", self.reverse_image_cache_root),
        )
        try:
            for name, value in roots:
                if value is None:
                    raise StorageRootError(f"{name}_missing")
                setattr(self, name, validate_controlled_root(value, create=True, writable=True))
        except StorageRootError:
            # 不把宿主绝对路径写入启动日志，只向调用方保留稳定错误码。
            raise

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
            "agent_executor_configured": bool(self.agent_executor_url),
            "agent_executor_token_configured": bool(self.agent_executor_token),
            "agent_runtime_mode": self.agent_runtime_mode,
            "data_root_configured": True,
            "opencode_concurrency": self.opencode_concurrency,
            "agent_backpressure": self.agent_backpressure,
            "settings_version": self.settings_version,
            "embedding_dimensions": self.embedding_dimensions,
            "visual_model": self.visual_model,
            "visual_model_dimensions": self.visual_model_dimensions,
            "visual_preprocess_version": self.visual_preprocess_version,
            "visual_available": self.visual_available,
            "database_configured": bool(self.database_url),
            "worker_lease_seconds": self.worker_lease_seconds,
            "worker_heartbeat_seconds": self.worker_heartbeat_seconds,
        }

    def backend_status(self, *, cache_ready: bool = False, runtime_ready: bool = False) -> dict[str, object]:
        """生成后端设置页三类字段、待生效值和重启提示。"""
        pending = self.pending_concurrency
        environment_override = bool(self.environment_overrides)
        restart_required = pending is not None and pending != self.opencode_concurrency and not environment_override
        readonly = {
            "embedding_model": self.embedding_model,
            "visual_model": self.visual_model,
            "visual_model_dimensions": self.visual_model_dimensions,
            "visual_preprocess_version": self.visual_preprocess_version,
            "visual_available": self.visual_available,
            "opencode_model": self.opencode_model,
            "agent_runtime_mode": self.agent_runtime_mode,
            "agent_executor_configured": bool(self.agent_executor_url),
            "agent_executor_token_configured": bool(self.agent_executor_token),
            "opencode_configured": bool(self.opencode_executable and self.opencode_model and self.opencode_base_url and self.opencode_api_key),
            "embedding_cache_ready": cache_ready,
            "reverse_image_available": bool(self.serpapi_api_key),
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

    @property
    def visual_available(self) -> bool:
        """返回官方源码和权重是否可读且（如配置）通过 SHA 校验，不暴露本地路径。"""
        path = self.visual_weights_path
        source = self.visual_model_repo
        if path is None or source is None:
            return False
        try:
            spec = visual_model_spec(self.visual_model)
            if spec is None or not spec.runtime_supported:
                return False
            source_root = source.expanduser()
            if not source_repository_valid(source_root, self.visual_model):
                return False
            resolved = path.expanduser()
            if not resolved.is_file() or not os.access(resolved, os.R_OK):
                return False
            if self.visual_weights_sha256:
                digest = hashlib.sha256()
                with resolved.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                return digest.hexdigest().lower() == self.visual_weights_sha256.lower()
            return True
        except OSError:
            return False


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
