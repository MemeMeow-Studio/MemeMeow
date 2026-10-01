# 图片文本向量的共享输入规则，供上传、名称更新、搜索和修复共同使用。

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from backend.image_naming import SUPPORTED_IMAGE_EXTENSIONS
from backend.metadata import MAX_SEMANTIC_DOCUMENT_LENGTH, MemeContext, semantic_document


TEXT_INPUT_VERSION = "name-context-v1"
_HASH_NAME = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_.-]*")


def filename_text(display_name: str) -> str:
    """规范化展示名称并过滤完整哈希或纯分隔符，返回可贡献的名称文本。"""
    name = " ".join(unicodedata.normalize("NFKC", display_name).split())
    while True:
        extension = next((item for item in SUPPORTED_IMAGE_EXTENSIONS if name.lower().endswith(item)), None)
        if extension is None:
            break
        name = name[:-len(extension)].rstrip()
    if _HASH_NAME.fullmatch(name) or _SEPARATORS.fullmatch(name):
        return ""
    return name


@dataclass(frozen=True)
class TextInput:
    """当前名称和有效语境产生的文本、版本指纹及等待原因。"""

    text: str
    fingerprint: str | None
    status: str


def build_text_input(display_name: str, context: object, context_status: str) -> TextInput:
    """根据当前名称及语境资格构造输入；图片身份验证由调用入口负责。"""
    if context_status == "repair_required":
        return TextInput("", None, "repair_required")
    chunks = []
    name = filename_text(display_name)
    if name:
        chunks.append(f"名称：{name}")
    if context_status in {"partial", "ready"}:
        try:
            parsed = context if isinstance(context, MemeContext) else MemeContext.model_validate(context)
        except (TypeError, ValueError):
            # 无效语境不贡献文本；经过身份校验的名称仍有独立索引资格。
            parsed = None
        if parsed is not None:
            document = semantic_document(parsed)
            if document:
                chunks.append(document)
    document = "\n".join(chunks)[:MAX_SEMANTIC_DOCUMENT_LENGTH]
    if not document:
        return TextInput("", None, "waiting_for_text")
    fingerprint = hashlib.sha256(f"{TEXT_INPUT_VERSION}\n{document}".encode("utf-8")).hexdigest()
    return TextInput(document, fingerprint, "eligible")
