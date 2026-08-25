"""视觉向量校验和 scope 绑定持久化访问。

该模块位于持久化 Repository 边界，负责视觉向量的模型空间身份、图片 SHA、
Agent 语境资格和 pgvector 匹配；视觉推理、任务编排、HTTP 和文件存储由其它
模块负责，旧 `backend.database` 路径由 facade 保留。
"""

from __future__ import annotations

import math
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.persistence.engine import DatabaseError
from backend.persistence.models import (
    VISUAL_EMBEDDING_DIMENSIONS,
    Meme,
    MemeVisualEmbedding,
    ScopeContext,
    StorageOperation,
    utcnow,
)


def validate_visual_vector(vector: Sequence[float], *, dimensions: int = VISUAL_EMBEDDING_DIMENSIONS) -> list[float]:
    """校验视觉向量维度、有限性和范数，并返回 L2 归一化的普通列表。"""
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise DatabaseError("visual_embedding_invalid") from exc
    if len(values) != int(dimensions):
        raise DatabaseError("visual_embedding_dimensions_mismatch")
    if not all(math.isfinite(value) for value in values):
        raise DatabaseError("visual_embedding_non_finite")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise DatabaseError("visual_embedding_zero_norm")
    return [value / norm for value in values]


class VisualEmbeddingRepository:
    """绑定 scope 的视觉向量写入和精确 cosine 查询 repository。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    @staticmethod
    def _identity(model: str, preprocess_version: str, dimensions: int) -> tuple[str, str, int]:
        """规范化向量空间身份，避免空模型或错误维度进入查询。"""
        if not isinstance(model, str) or not model.strip() or not isinstance(preprocess_version, str) or not preprocess_version.strip():
            raise DatabaseError("visual_model_not_configured")
        if int(dimensions) <= 0:
            raise DatabaseError("visual_embedding_dimensions_mismatch")
        return model.strip(), preprocess_version.strip(), int(dimensions)

    def get(
        self,
        meme_id: UUID | str,
        *,
        model: str,
        preprocess_version: str,
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
        image_sha256: str | None = None,
        for_update: bool = False,
    ) -> MemeVisualEmbedding | None:
        """读取当前 scope、模型空间和可选图片 SHA 对应的视觉向量。"""
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError):
            return None
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        filters = [
            MemeVisualEmbedding.scope_id == self.scope.scope_id,
            MemeVisualEmbedding.meme_id == identifier,
            MemeVisualEmbedding.model == model,
            MemeVisualEmbedding.preprocess_version == preprocess_version,
            MemeVisualEmbedding.dimensions == dimensions,
        ]
        if image_sha256 is not None:
            filters.append(MemeVisualEmbedding.image_sha256 == image_sha256)
        statement = select(MemeVisualEmbedding).where(*filters)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def upsert(
        self,
        meme_id: UUID | str,
        *,
        model: str,
        preprocess_version: str,
        image_sha256: str,
        embedding: Sequence[float],
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
    ) -> MemeVisualEmbedding:
        """校验并幂等写入当前图片版本的视觉向量。"""
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        normalized = validate_visual_vector(embedding, dimensions=dimensions)
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError) as exc:
            raise DatabaseError("meme_not_found") from exc
        if not isinstance(image_sha256, str) or len(image_sha256) != 64 or any(character not in "0123456789abcdefABCDEF" for character in image_sha256):
            raise DatabaseError("visual_embedding_sha256_invalid")
        meme = self.session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == identifier).with_for_update())
        if meme is None:
            raise DatabaseError("meme_not_found")
        if meme.sha256 != image_sha256:
            raise DatabaseError("visual_embedding_sha256_mismatch")
        row = self.get(identifier, model=model, preprocess_version=preprocess_version, dimensions=dimensions, for_update=True)
        if row is None:
            row = MemeVisualEmbedding(scope_id=self.scope.scope_id, meme_id=identifier, model=model, preprocess_version=preprocess_version, dimensions=dimensions, image_sha256=image_sha256, embedding=normalized)
            self.session.add(row)
        else:
            row.image_sha256 = image_sha256
            row.embedding = normalized
            row.dimensions = dimensions
            row.updated_at = utcnow()
        self.session.flush()
        return row

    @staticmethod
    def agent_ready(meme: Meme) -> bool:
        """验证候选图片具有当前 SHA 对应的 research Agent provenance。"""
        if meme.context_status != "ready":
            return False
        summary = (meme.provenance or {}).get("agent_context")
        if not isinstance(summary, dict):
            return False
        return bool(
            summary.get("task_id")
            and summary.get("model")
            and summary.get("skill_hash")
            and summary.get("completed_at")
            and summary.get("image_sha256") == meme.sha256
        )

    def match(
        self,
        vector: Sequence[float],
        *,
        model: str,
        preprocess_version: str,
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
        limit: int = 20,
        exclude_meme_id: UUID | str | None = None,
    ) -> list[tuple[MemeVisualEmbedding, Meme, float]]:
        """在当前 scope 内精确查询合格候选并按分数和 Meme UUID 稳定排序。"""
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        normalized = validate_visual_vector(vector, dimensions=dimensions)
        limit = max(1, min(int(limit), 50))
        excluded: UUID | None = None
        if exclude_meme_id is not None:
            try:
                excluded = UUID(str(exclude_meme_id))
            except (ValueError, TypeError):
                excluded = None
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        distance = MemeVisualEmbedding.embedding.cosine_distance(normalized)
        statement = (
            select(MemeVisualEmbedding, Meme, (1 - distance).label("score"))
            .join(Meme, (Meme.scope_id == MemeVisualEmbedding.scope_id) & (Meme.id == MemeVisualEmbedding.meme_id))
            .where(
                MemeVisualEmbedding.scope_id == self.scope.scope_id,
                MemeVisualEmbedding.model == model,
                MemeVisualEmbedding.preprocess_version == preprocess_version,
                MemeVisualEmbedding.dimensions == dimensions,
                MemeVisualEmbedding.image_sha256 == Meme.sha256,
                ~active_operation,
            )
            .order_by(distance.asc(), MemeVisualEmbedding.meme_id.asc())
            .limit(max(limit * 8, 50))
        )
        if excluded is not None:
            statement = statement.where(MemeVisualEmbedding.meme_id != excluded)
        rows: list[tuple[MemeVisualEmbedding, Meme, float]] = []
        for embedding, meme, score in self.session.execute(statement).all():
            if not self.agent_ready(meme):
                continue
            rows.append((embedding, meme, float(score)))
            if len(rows) >= limit:
                break
        rows.sort(key=lambda item: (-item[2], str(item[1].id)))
        return rows

    def query(self, vector: Sequence[float], **kwargs: Any) -> list[tuple[UUID, float]]:
        """返回与文本 SearchRepository 对齐的 ``(meme_id, score)`` 结果。"""
        return [(meme.id, score) for _embedding, meme, score in self.match(vector, **kwargs)]


__all__ = ["VisualEmbeddingRepository", "validate_visual_vector"]
