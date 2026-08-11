"""图片 sidecar 元数据服务。

该模块位于后端文件库与检索、标注服务之间，负责 meme_context 的 schema 校验、
图片指纹、生命周期同步和可恢复写入；embedding 向量不存放在 sidecar 中。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.paths import SUPPORTED_EXTENSIONS


SCHEMA_VERSION = 1
CONTEXT_STATUSES = {"pending", "partial", "ready", "repair_required"}
EMBEDDING_FIELDS = ("summary", "subjects", "visible_text", "references", "meaning", "keywords")
MAX_CONTEXT_ITEMS = 64
MAX_CONTEXT_ITEM_LENGTH = 500
MAX_SUMMARY_LENGTH = 2000
MAX_SEMANTIC_DOCUMENT_LENGTH = 6000


def _now() -> str:
    """返回用于元数据审计的 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


def _clean_list(values: list[str]) -> list[str]:
    """校验并去重字符串数组，保留数组的稳定顺序。"""
    if not isinstance(values, list):
        raise ValueError("context_items_must_be_array")
    if len(values) > MAX_CONTEXT_ITEMS:
        raise ValueError(f"context_items_limit:{MAX_CONTEXT_ITEMS}")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("context_item_must_be_string")
        value = value.strip()
        if not value:
            continue
        if len(value) > MAX_CONTEXT_ITEM_LENGTH:
            raise ValueError(f"context_item_too_long:{MAX_CONTEXT_ITEM_LENGTH}")
        if value not in result:
            result.append(value)
    return result


class MemeContext(BaseModel):
    """研究输出中的图片语境字段，未知扩展字段会被保留。"""

    model_config = ConfigDict(extra="allow")

    summary: str = Field(default="", max_length=MAX_SUMMARY_LENGTH)
    subjects: list[str] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    meaning: str | None = Field(default=None, max_length=MAX_CONTEXT_ITEM_LENGTH)
    keywords: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)

    _clean_subjects = field_validator("subjects", "visible_text", "references", "keywords", "search_queries", "uncertainties", mode="before")(_clean_list)

    @field_validator("summary", mode="before")
    @classmethod
    def clean_summary(cls, value: str | None) -> str:
        """清理摘要外围空白，保留空摘要供 pending 状态使用。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("context_text_must_be_string")
        return value.strip()

    @field_validator("meaning", mode="before")
    @classmethod
    def clean_meaning(cls, value: str | None) -> str | None:
        """清理含义外围空白，空值统一为 null。"""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("context_text_must_be_string")
        return value.strip() or None

    @field_validator("source_urls", mode="before")
    @classmethod
    def validate_source_urls(cls, values: list[str]) -> list[str]:
        """只接受可回查的 HTTP(S) 来源 URL。"""
        cleaned = _clean_list(values)
        for value in cleaned:
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("source_url_must_be_http_uri")
        return cleaned


class ImageIdentity(BaseModel):
    """sidecar 绑定的图片身份和内容指纹。"""

    model_config = ConfigDict(extra="allow")

    relative_path: str
    extension: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)


class Provenance(BaseModel):
    """记录元数据生产者、时间及失败状态。"""

    model_config = ConfigDict(extra="allow")

    producer: str = "system"
    model: str | None = None
    updated_at: str = Field(default_factory=_now)
    field_sources: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None


class SidecarMetadata(BaseModel):
    """图片 sidecar 的版本化外层结构。"""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    image: ImageIdentity
    context_status: Literal["pending", "partial", "ready", "repair_required"] = "pending"
    meme_context: MemeContext = Field(default_factory=MemeContext)
    provenance: Provenance = Field(default_factory=Provenance)


class MetadataError(RuntimeError):
    """携带稳定错误标识的元数据异常。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def semantic_document(context: MemeContext) -> str:
    """按固定字段白名单构造 embedding 文本，排除研究辅助字段。"""
    sections: list[tuple[str, str | list[str] | None]] = [
        ("摘要", context.summary),
        ("主体", context.subjects),
        ("图片文字", context.visible_text),
        ("已确认引用", context.references),
        ("常见含义", context.meaning),
        ("关键词", context.keywords),
    ]
    chunks: list[str] = []
    for title, value in sections:
        if isinstance(value, list):
            value = "；".join(value)
        if value:
            chunks.append(f"{title}：{value}")
    return "\n".join(chunks)[:MAX_SEMANTIC_DOCUMENT_LENGTH]


class MetadataService:
    """管理图片 sidecar 的读写、校验、生命周期和检索文本。"""

    def __init__(self, image_root: Path):
        self.root = image_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _relative(self, image: Path) -> str:
        """返回图片在受控根目录下的 POSIX 相对路径。"""
        try:
            return image.resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise MetadataError("path_forbidden") from exc

    def sidecar_path(self, image: Path) -> Path:
        """根据图片完整文件名计算同目录 sidecar 路径。"""
        self._relative(image)
        return image.with_name(f"{image.name}.json")

    def image_sha256(self, image: Path) -> str:
        """计算图片内容 SHA-256，用于检测 sidecar 与图片是否错配。"""
        digest = hashlib.sha256()
        try:
            with image.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise MetadataError("image_unreadable") from exc
        return digest.hexdigest()

    def _identity(self, image: Path) -> dict[str, object]:
        """构造当前图片的身份字段。"""
        try:
            stat = image.stat()
        except OSError as exc:
            raise MetadataError("image_unreadable") from exc
        return {
            "relative_path": self._relative(image),
            "extension": image.suffix.lower(),
            "size_bytes": stat.st_size,
            "sha256": self.image_sha256(image),
        }

    def _base(self, image: Path, status: str = "pending") -> dict[str, object]:
        """为新图片创建最小且符合研究 schema 的元数据。"""
        if status not in CONTEXT_STATUSES:
            raise MetadataError("invalid_context_status")
        return {
            "schema_version": SCHEMA_VERSION,
            "image": self._identity(image),
            "context_status": status,
            "meme_context": {
                "summary": "",
                "subjects": [],
                "visible_text": [],
                "references": [],
                "meaning": None,
                "keywords": [],
                "search_queries": [],
                "uncertainties": ["尚未完成图片语境研究"],
                "source_urls": [],
            },
            "provenance": {
                "producer": "system",
                "model": None,
                "updated_at": _now(),
                "field_sources": {},
                "last_error": None,
            },
        }

    def _atomic_write(self, sidecar: Path, payload: dict[str, object]) -> None:
        """通过同目录临时文件和原子替换提交完整 JSON。"""
        temporary = sidecar.with_name(f".{sidecar.name}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, sidecar)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise MetadataError("metadata_write_failed") from exc

    def _validate(self, image: Path, payload: dict[str, object]) -> SidecarMetadata:
        """校验 JSON 结构、路径和图片指纹。"""
        try:
            metadata = SidecarMetadata.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise MetadataError("metadata_invalid") from exc
        current = self._identity(image)
        if metadata.image.relative_path != current["relative_path"]:
            raise MetadataError("metadata_path_mismatch")
        if metadata.image.extension != current["extension"] or metadata.image.sha256 != current["sha256"] or metadata.image.size_bytes != current["size_bytes"]:
            raise MetadataError("metadata_image_mismatch")
        return metadata

    def load(self, image: Path) -> SidecarMetadata:
        """读取并严格校验一张图片的 sidecar。"""
        sidecar = self.sidecar_path(image)
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MetadataError("metadata_missing") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise MetadataError("metadata_invalid") from exc
        return self._validate(image, payload)

    def status(self, image: Path) -> dict[str, object]:
        """返回适合图片库展示的元数据状态和安全摘要。"""
        try:
            metadata = self.load(image)
        except MetadataError as exc:
            return {"status": "repair_required", "error": exc.code}
        context = metadata.meme_context
        return {
            "status": metadata.context_status,
            "summary": context.summary,
            "subjects": context.subjects,
            "meaning": context.meaning,
            "keywords": context.keywords,
        }

    def create_pending(self, image: Path) -> SidecarMetadata:
        """为图片创建初始 pending sidecar；已有合法记录不会被覆盖。"""
        try:
            return self.load(image)
        except MetadataError as exc:
            if exc.code not in {"metadata_missing", "metadata_invalid", "metadata_path_mismatch", "metadata_image_mismatch"}:
                raise
        payload = self._base(image)
        sidecar = self.sidecar_path(image)
        self._atomic_write(sidecar, payload)
        return SidecarMetadata.model_validate(payload)

    def write(self, image: Path, payload: dict[str, object]) -> SidecarMetadata:
        """校验并原子写入 sidecar，同时刷新图片身份字段。"""
        payload = dict(payload)
        payload["schema_version"] = SCHEMA_VERSION
        payload["image"] = self._identity(image)
        metadata = SidecarMetadata.model_validate(payload)
        serialized = metadata.model_dump(mode="json", exclude_none=False)
        self._atomic_write(self.sidecar_path(image), serialized)
        return metadata

    def update_context(
        self,
        image: Path,
        context_updates: dict[str, object],
        *,
        producer: str,
        model: str | None = None,
        status: str = "partial",
        error: str | None = None,
    ) -> SidecarMetadata:
        """合并语境字段并记录来源；自动流程不会覆盖人工字段。"""
        try:
            current = self.load(image)
        except MetadataError:
            current = self.create_pending(image)
        payload = current.model_dump(mode="json", exclude_none=False)
        context = dict(payload.get("meme_context") or {})
        sources = dict((payload.get("provenance") or {}).get("field_sources") or {})
        for field, value in context_updates.items():
            if field not in MemeContext.model_fields:
                continue
            if sources.get(field) == "human" and producer != "human":
                continue
            context[field] = value
            sources[field] = producer
        payload["meme_context"] = context
        payload["context_status"] = status if status in CONTEXT_STATUSES else "partial"
        provenance = dict(payload.get("provenance") or {})
        provenance.update({"producer": producer, "model": model, "updated_at": _now(), "field_sources": sources, "last_error": error})
        payload["provenance"] = provenance
        return self.write(image, payload)

    def apply_visual_candidates(self, image: Path, candidates: list[str], model: str | None = None) -> SidecarMetadata:
        """将视觉模型候选转为画面事实字段，不推定外部引用和含义。"""
        values = [value.strip() for value in candidates if isinstance(value, str) and value.strip()]
        if not values:
            raise MetadataError("metadata_context_empty")
        return self.update_context(image, {"summary": values[0], "keywords": values}, producer="vision", model=model, status="partial", error=None)

    def record_error(self, image: Path, *, producer: str, model: str | None, error: str) -> SidecarMetadata:
        """保留已有语境并记录本次生成失败，供任务重试和诊断使用。"""
        try:
            current = self.load(image)
        except MetadataError:
            current = self.create_pending(image)
        payload = current.model_dump(mode="json", exclude_none=False)
        provenance = dict(payload.get("provenance") or {})
        provenance.update({"producer": producer, "model": model, "updated_at": _now(), "last_error": error})
        payload["provenance"] = provenance
        return self.write(image, payload)

    def rename(self, source: Path, target: Path) -> SidecarMetadata:
        """同步重命名图片和 sidecar，失败时回滚图片路径。"""
        if target.exists() and target != source:
            raise MetadataError("target_exists")
        old_sidecar = self.sidecar_path(source)
        new_sidecar = self.sidecar_path(target)
        if new_sidecar.exists() and new_sidecar != old_sidecar:
            raise MetadataError("target_exists")
        try:
            metadata = self.load(source)
        except MetadataError as exc:
            if exc.code not in {"metadata_missing", "metadata_invalid", "metadata_path_mismatch", "metadata_image_mismatch"}:
                raise
            metadata = self.create_pending(source)
        source.rename(target)
        try:
            self.write(target, metadata.model_dump(mode="json", exclude_none=False))
            if old_sidecar != new_sidecar:
                old_sidecar.unlink(missing_ok=True)
            return self.load(target)
        except Exception as exc:  # noqa: BLE001
            try:
                new_sidecar.unlink(missing_ok=True)
                target.rename(source)
            except OSError as rollback_error:
                raise MetadataError("rename_rollback_failed") from rollback_error
            if isinstance(exc, MetadataError):
                raise
            raise MetadataError("metadata_rename_failed") from exc

    def remove(self, image: Path) -> None:
        """删除图片及其 sidecar；sidecar 删除失败时恢复图片内容。"""
        sidecar = self.sidecar_path(image)
        try:
            content = image.read_bytes()
            image.unlink()
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            if not image.exists():
                try:
                    image.write_bytes(content)
                except OSError:
                    raise MetadataError("image_delete_rollback_failed") from exc
            raise MetadataError("image_delete_failed") from exc

    def embedding_record(self, image: Path) -> dict[str, object]:
        """返回构造索引所需的语义文本、状态和指纹。"""
        image_sha = self.image_sha256(image)
        try:
            metadata = self.load(image)
        except MetadataError as exc:
            stem = image.stem.replace("-", " ").replace("_", " ").strip() or image.stem
            return {"text": stem, "status": "repair_required", "metadata_schema_version": None, "metadata_hash": None, "image_sha256": image_sha, "error": exc.code}
        context = metadata.meme_context
        text = semantic_document(context) if metadata.context_status in {"partial", "ready"} else ""
        if not text:
            text = image.stem.replace("-", " ").replace("_", " ").strip() or image.stem
        serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "text": text,
            "status": metadata.context_status,
            "metadata_schema_version": metadata.schema_version,
            "metadata_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "image_sha256": image_sha,
        }

    def repair(self, progress: Any | None = None) -> dict[str, object]:
        """幂等补齐缺失或损坏 sidecar，并报告孤立文件。"""
        paths = sorted((path for path in self.root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS), key=lambda path: self._relative(path))
        counts = {"processed": 0, "created": 0, "repaired": 0, "pending": 0, "partial": 0, "ready": 0, "repair_required": 0}
        for index, image in enumerate(paths, start=1):
            counts["processed"] += 1
            sidecar_exists = self.sidecar_path(image).is_file()
            try:
                metadata = self.load(image)
            except MetadataError:
                self.create_pending(image)
                counts["repaired" if sidecar_exists else "created"] += 1
                metadata = self.load(image)
            counts[metadata.context_status] += 1
            if progress:
                progress(index / max(len(paths), 1), f"正在整理元数据 {index}/{len(paths)}")
        known = {self.sidecar_path(path).resolve() for path in paths}
        orphaned = [path.as_posix() for path in self.root.rglob("*.json") if path.is_file() and path.resolve() not in known]
        counts["orphaned"] = len(orphaned)
        counts["orphan_paths"] = orphaned[:100]
        return counts
