"""服务端运行配置。

该模块集中读取 `.env` 和进程环境变量，避免前端或 YAML 配置接口接触模型密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """FastAPI 进程级配置，实例在应用生命周期内保持不变。"""

    data_root: Path
    image_root: Path
    embedding_api_key: str | None
    embedding_base_url: str | None
    embedding_model: str
    llm_enhance_model: str | None
    vlm_api_key: str | None
    vlm_base_url: str | None
    vlm_model: str
    vlm_max_attempts: int
    protected_mode: bool
    allowed_endpoints: tuple[str, ...]
    rate_limit_enabled: bool
    rate_limit_requests: int
    rate_limit_window: int
    max_upload_size: int

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        """从 `.env` 加载配置；显式进程环境变量优先于文件内容。"""
        load_dotenv(env_file, override=False)
        project_root = Path(__file__).resolve().parent.parent
        data_root = Path(os.getenv("MEMEMEOW_DATA_ROOT", str(project_root / "data"))).expanduser()
        image_root = Path(os.getenv("MEMEMEOW_IMAGE_ROOT", str(data_root / "images"))).expanduser()

        def boolean(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

        allowed = tuple(p.strip() for p in os.getenv("MEMEMEOW_ALLOWED_ENDPOINTS", "/,/health,/search,/config,/tasks").split(",") if p.strip())
        return cls(
            data_root=data_root,
            image_root=image_root,
            embedding_api_key=os.getenv("EMBEDDING_API_KEY") or None,
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL") or None,
            embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
            llm_enhance_model=os.getenv("LLM_ENHANCE_MODEL") or None,
            vlm_api_key=os.getenv("VLM_API_KEY") or None,
            vlm_base_url=os.getenv("VLM_BASE_URL") or None,
            vlm_model=os.getenv("VLM_MODEL", "Qwen/Qwen2-VL-72B-Instruct"),
            vlm_max_attempts=min(3, max(1, int(os.getenv("VLM_MAX_ATTEMPTS", "2")))),
            protected_mode=boolean("MEMEMEOW_PROTECTED_MODE", False),
            allowed_endpoints=allowed,
            rate_limit_enabled=boolean("MEMEMEOW_RATE_LIMIT_ENABLED", False),
            rate_limit_requests=max(1, int(os.getenv("MEMEMEOW_RATE_LIMIT_REQUESTS", "60"))),
            rate_limit_window=max(1, int(os.getenv("MEMEMEOW_RATE_LIMIT_WINDOW", "60"))),
            max_upload_size=max(1, int(os.getenv("MEMEMEOW_MAX_UPLOAD_SIZE", str(20 * 1024 * 1024)))),
        )

    def ensure_directories(self) -> None:
        """创建受控数据目录，应用启动时调用。"""
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.image_root.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, object]:
        """返回可公开给前端的脱敏配置状态。"""
        return {
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key_configured": bool(self.embedding_api_key),
            "llm_enhance_model": self.llm_enhance_model,
            "vlm_model": self.vlm_model,
            "vlm_base_url": self.vlm_base_url,
            "vlm_api_key_configured": bool(self.vlm_api_key),
            "data_root_configured": True,
        }
