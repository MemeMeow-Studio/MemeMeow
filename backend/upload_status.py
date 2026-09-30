# 上传状态与重试规则，供任务查询和显式重试共用。

from collections.abc import Mapping
from typing import Any


SAVED_UPLOAD_PHASES = frozenset({"submitting", "submitted"})
PERMANENT_UPLOAD_ERRORS = frozenset({
    "image_exists", "file_exists", "invalid_image", "unsupported_format", "invalid_filename",
    "empty_file", "file_too_large", "image_frame_count_exceeded", "image_frame_pixels_exceeded",
    "image_total_pixels_exceeded", "upload_request_conflict", "upload_input_size_changed",
    "upload_reconciliation_required", "upload_receiving_interrupted", "upload_receipt_missing", "task_cancelled",
    "gif_processing_memory_exceeded", "gif_compression_output_limit_exceeded",
    "gif_compression_animation_changed", "gif_compression_transparency_changed", "gif_compression_target_exceeded",
})


def upload_retry_error(status: str, error: Mapping[str, Any] | None, receipt: Mapping[str, Any] | None) -> str | None:
    """根据任务和接收快照返回禁止重试的原因；None 表示可以重试。"""
    if status != "failed":
        return "task_not_failed"
    code = (error or {}).get("error")
    if code in PERMANENT_UPLOAD_ERRORS:
        return str(code)
    if receipt is None or receipt.get("input_removed"):
        return "upload_input_expired"
    return None
