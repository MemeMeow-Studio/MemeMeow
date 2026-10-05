# 检索图片尺寸输出：复用派生文件记录、身份锁和清理流程，保留原图格式与动画。

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.gif_compression import GifCompressionError, MAX_GIF_OUTPUT_BYTES, gif_preflight_resources
from backend.image_export_worker import image_export_worker
from backend.persistence.engine import DatabaseError
from backend.persistence.models import Meme
from backend.services.thumbnails import DerivedThumbnailService, RenderedThumbnail, ThumbnailError, _render_thumbnail
from backend.thumbnail_config import ThumbnailConfig


IMAGE_EXPORT_EDGES = (60, 120, 240, 360, 480, 640)
EXPORT_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


@dataclass(frozen=True, slots=True)
class ImageExportConfig(ThumbnailConfig):
    """尺寸输出的固定档位与资源预算；profile 同时区分算法版本和最长边。"""

    profile: str = "export-v1-480"
    max_edge: int = 480
    max_output_bytes: int = MAX_GIF_OUTPUT_BYTES

    def __post_init__(self) -> None:
        """只允许公开档位和对应缓存身份，沿用派生图片资源预算校验。"""
        if type(self.max_edge) is not int or self.max_edge not in IMAGE_EXPORT_EDGES:
            raise ValueError("image_export_size_invalid")
        if self.profile != f"export-v1-{self.max_edge}":
            raise ValueError("image_export_profile_invalid")
        self._validate_resources()


def render_image_export(content: bytes, extension: str, config: ImageExportConfig) -> RenderedThumbnail:
    """使用父进程临时目录生成输出；GIF 共享处理预算，超时回收编码器。"""
    try:
        with gif_preflight_resources(extension.lower() == ".gif") as directory:
            worker = partial(image_export_worker, directory=directory)
            return _render_thumbnail(content, extension, config, worker=worker)
    except GifCompressionError as exc:
        raise ThumbnailError(exc.code) from exc


class DerivedImageExportService(DerivedThumbnailService):
    """当前 scope 的按需尺寸输出，使用既有派生记录与原图删除流程。"""

    def __init__(self, thumbnails: DerivedThumbnailService, max_edge: int) -> None:
        """从当前 scope 的派生服务共享资源与并发预算，绑定本次请求的尺寸。"""
        config = ImageExportConfig(
            profile=f"export-v1-{max_edge}", max_edge=max_edge,
            timeout_seconds=thumbnails.config.timeout_seconds, max_temp_bytes=thumbnails.config.max_temp_bytes,
        )
        super().__init__(
            thumbnails.resources, scope_id=thumbnails.scope, config=config,
            generation_slots=thumbnails.generation_slots,
        )

    @staticmethod
    def _output_key(meme: Meme, profile: str) -> str:
        """按源版本、档位和真实输出格式生成缓存 key，供既有删除流程收集。"""
        extension = ".jpg" if meme.extension.lower() == ".jpeg" else meme.extension.lower()
        return f"{meme.id.hex}-{str(meme.sha256).lower()}-{profile}{extension}"

    @staticmethod
    def _media_type(meme: Meme) -> str:
        """沿用原图格式，确保相册下载可以识别 PNG、JPEG 和 GIF。"""
        return EXPORT_MEDIA_TYPES[meme.extension.lower()]

    def _render(self, content: bytes, extension: str) -> RenderedThumbnail:
        """调用保持透明度和动画的尺寸生成器。"""
        return render_image_export(content, extension, self.config)

    def _projection(self, row: Any, *, meme_id: UUID | str) -> dict[str, object]:
        """为既有生成流程返回尺寸输出的受控 URL。"""
        payload = super()._projection(row, meme_id=meme_id)
        if payload["status"] == "available":
            payload["media_url"] = f"/media/{meme_id}?max_edge={self.config.max_edge}"
        return payload

    def prepare_media(self, meme_id: UUID | str) -> tuple[Path, str, str]:
        """复用可用缓存；缺少文件或记录时按需生成，再读取已提交的输出事实。"""
        try:
            return self.media_path(meme_id)
        except (ThumbnailError, DatabaseError) as exc:
            if exc.code not in {"thumbnail_not_found", "file_not_found"}:
                raise
        self.generate(meme_id)
        return self.media_path(meme_id)
