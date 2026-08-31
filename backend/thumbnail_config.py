"""缩略图派生配置。

该模块位于服务配置与缩略图生成服务之间，集中校验展示 profile、输出预算和
并发背压边界，避免不同入口各自解释同一组限制。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


THUMBNAIL_PROFILE = "thumbnail-v1"
THUMBNAIL_MAX_EDGE = 320
THUMBNAIL_OUTPUT_MEDIA_TYPE = "image/jpeg"
THUMBNAIL_OUTPUT_EXTENSION = ".jpg"
# 缩略图优先控制体积；质量 70 在 320px 展示尺寸下保留可接受的细节。
THUMBNAIL_JPEG_QUALITY = 70


@dataclass(frozen=True, slots=True)
class ThumbnailConfig:
    """缩略图生成的固定展示规则和资源预算。

    ``profile``、``max_edge`` 和输出格式共同构成派生身份；其余字段限制单次
    生成可以消耗的时间、临时内存近似值、输出字节和队列压力。
    """

    profile: str = THUMBNAIL_PROFILE
    max_edge: int = THUMBNAIL_MAX_EDGE
    max_output_bytes: int = 4 * 1024 * 1024
    timeout_seconds: float = 10.0
    max_temp_bytes: int = 64 * 1024 * 1024
    concurrency: int = 2
    backpressure: int = 100
    reconcile_batch_size: int = 100

    def __post_init__(self) -> None:
        """拒绝会改变派生契约或绕过资源预算的配置。"""
        if not isinstance(self.profile, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.profile):
            raise ValueError("thumbnail_profile_invalid")
        if self.profile != THUMBNAIL_PROFILE:
            raise ValueError("thumbnail_profile_mismatch")
        if self.max_edge != THUMBNAIL_MAX_EDGE:
            raise ValueError("thumbnail_max_edge_mismatch")
        if not isinstance(self.max_output_bytes, int) or not 1 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ValueError("thumbnail_output_limit_invalid")
        if not isinstance(self.timeout_seconds, (int, float)) or not 0 < float(self.timeout_seconds) <= 120:
            raise ValueError("thumbnail_timeout_invalid")
        if not isinstance(self.max_temp_bytes, int) or not 1 <= self.max_temp_bytes <= 512 * 1024 * 1024:
            raise ValueError("thumbnail_temp_limit_invalid")
        if not isinstance(self.concurrency, int) or not 1 <= self.concurrency <= 32:
            raise ValueError("thumbnail_concurrency_invalid")
        if not isinstance(self.backpressure, int) or self.backpressure < self.concurrency or self.backpressure > 10_000:
            raise ValueError("thumbnail_backpressure_invalid")
        if not isinstance(self.reconcile_batch_size, int) or not 1 <= self.reconcile_batch_size <= 1_000:
            raise ValueError("thumbnail_reconcile_batch_invalid")

    @classmethod
    def from_settings(cls, settings: Any) -> "ThumbnailConfig":
        """从服务端设置读取唯一的缩略图配置来源。"""
        return cls(
            profile=getattr(settings, "thumbnail_profile", THUMBNAIL_PROFILE),
            max_edge=int(getattr(settings, "thumbnail_max_edge", THUMBNAIL_MAX_EDGE)),
            max_output_bytes=int(getattr(settings, "thumbnail_max_output_bytes", 4 * 1024 * 1024)),
            timeout_seconds=float(getattr(settings, "thumbnail_timeout_seconds", 10.0)),
            max_temp_bytes=int(getattr(settings, "thumbnail_max_temp_bytes", 64 * 1024 * 1024)),
            concurrency=int(getattr(settings, "thumbnail_concurrency", 2)),
            backpressure=int(getattr(settings, "thumbnail_backpressure", 100)),
            reconcile_batch_size=int(getattr(settings, "thumbnail_reconcile_batch_size", 100)),
        )


__all__ = [
    "THUMBNAIL_MAX_EDGE",
    "THUMBNAIL_JPEG_QUALITY",
    "THUMBNAIL_OUTPUT_EXTENSION",
    "THUMBNAIL_OUTPUT_MEDIA_TYPE",
    "THUMBNAIL_PROFILE",
    "ThumbnailConfig",
]
