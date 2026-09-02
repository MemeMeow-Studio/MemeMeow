from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import numpy as np
from openai import OpenAI

from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    Meme,
    ScopeContext,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.metadata import (
    MetadataError,
    MemeContext,
    semantic_document,
    semantic_document_hash,
)
from backend.services.metadata import PostgresMetadataService

# 检索服务沿用旧 logger 名称，避免服务拆分改变日志路由。
logger = logging.getLogger("backend.pg_services")

class PostgresSearchService:
    """基于 search_generations/meme_embeddings 的 pgvector 检索服务。

    直接构造时的 local 默认值仅用于开源兼容夹具；生产请求通过 scope facade 获取。
    """

    def __init__(self, settings: Any, resources: DatabaseResources, metadata: PostgresMetadataService, *, scope_id: str | ScopeContext = "local"):
        """绑定模型配置、持久化资源、元数据服务和当前 scope。"""
        self.settings, self.resources, self.metadata = settings, resources, metadata
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.model = settings.embedding_model
        self._generation_lock = __import__("threading").Lock()

    def _client(self) -> OpenAI:
        """创建嵌入客户端，缺少配置时返回稳定错误。"""
        if not self.settings.embedding_api_key or not self.settings.embedding_base_url:
            raise RuntimeError("embedding_not_configured")
        return OpenAI(api_key=self.settings.embedding_api_key, base_url=self.settings.embedding_base_url)

    def _embedding(self, text: str) -> list[float]:
        """调用模型并拒绝非 1024 维或零向量响应。"""
        response = self._client().embeddings.create(model=self.model, input=text, encoding_format="float")
        vector = np.asarray(response.data[0].embedding, dtype=float)
        if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.linalg.norm(vector):
            raise RuntimeError("embedding_dimensions_mismatch")
        return (vector / np.linalg.norm(vector)).tolist()

    def has_cache(self) -> bool:
        """检查当前 scope/model 的唯一检索来源是否已有有效数据。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.search.source_mode(self.model) == "incremental" and environment.search.has_incremental(self.model)

    def invalidate_cache(self) -> None:
        """数据库索引不在进程内缓存；此方法保留兼容调用但无副作用。"""

    def mark_cache_invalidated(self, batch_id: object = None) -> None:
        """兼容旧批次回调，generation 由独立任务显式刷新。"""

    def valid_text_embedding_ids(self, memes: Sequence[Meme]) -> set[UUID]:
        """返回给定图片中通过当前 scope/model 全部指纹校验的文本向量 ID。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.search.valid_text_embedding_ids(self.model, memes)

    def generate_cache(self, progress: Callable[[float | None, str | None], None], claim: tuple[str, int, str] | None = None) -> dict[str, object]:
        """按当前可索引候选回填增量文本向量，完成后切换 scope 迁移状态。"""
        with self._generation_lock:
            preflight = self.resources.search_rebuild_preflight(self.scope)
            if preflight["blocking"]:
                logger.warning("search_rebuild_blocked scope=%s keys=%s", self.scope.scope_id, preflight["blocking_keys"])
                raise RuntimeError("search_rebuild_preflight_failed")
            candidates: list[tuple[UUID, int, str, str, str]] = []
            skipped = 0
            with self.resources.environment(self.scope.scope_id) as environment:
                environment.search.assert_claim(claim)
                for meme in environment.memes.list_all():
                    try:
                        image = self.metadata.blob_store.resolve(meme.storage_key)
                        identity = self.metadata._identity(image)
                        context = MemeContext.model_validate(meme.meme_context or {})
                    except (DatabaseError, MetadataError, ValueError):
                        skipped += 1
                        continue
                    if identity["sha256"] != meme.sha256 or identity["size_bytes"] != meme.size_bytes or meme.context_status not in {"partial", "ready"}:
                        skipped += 1
                        continue
                    text_value = semantic_document(context)
                    if not text_value:
                        skipped += 1
                        continue
                    metadata_hash = meme.search_metadata_hash or semantic_document_hash(context)
                    if metadata_hash is None:
                        skipped += 1
                        continue
                    candidates.append((meme.id, meme.revision, meme.sha256, text_value, metadata_hash))
                if not candidates:
                    raise RuntimeError("no_indexable_images")
                candidates.sort(key=lambda item: str(item[0]))
                state = environment.search.begin_incremental_backfill(self.model, total_count=len(candidates))
                epoch = state.epoch
            total = len(candidates)
            from backend.image_processing import SingleImageEmbeddingService

            embedding_service = SingleImageEmbeddingService(self.resources, scope_id=self.scope, model=self.model, embedder=self._embedding)
            for index, (meme_id, _revision, image_sha, text_value, metadata_hash) in enumerate(candidates, start=1):
                embedding_service.upsert(meme_id, image_sha256=image_sha, metadata_hash=metadata_hash, semantic_document=text_value)
                with self.resources.environment(self.scope.scope_id) as environment:
                    if not environment.search.record_incremental_backfill(epoch=epoch, completed_count=index, model=self.model):
                        raise DatabaseError("migration_epoch_changed")
                if progress:
                    progress(index / total, f"正在生成 pgvector 索引 {index}/{total}")
            with self.resources.environment(self.scope.scope_id) as environment:
                if not environment.search.switch_incremental_only(epoch=epoch, model=self.model):
                    raise DatabaseError("migration_incomplete")
            return {"indexed_count": total, "skipped_count": skipped, "epoch": epoch, "model": self.model, "dimensions": EMBEDDING_DIMENSIONS}

    def _enhance_query(self, query: str) -> str:
        """使用可选 LLM 改写查询；失败由 API 回退普通查询。"""
        if not self.settings.llm_enhance_model:
            raise RuntimeError("llm_enhance_not_configured")
        response = self._client().chat.completions.create(model=self.settings.llm_enhance_model, messages=[{"role": "user", "content": f"将下面内容改写为一句适合检索表情包的简短描述，只输出描述：{query}"}], temperature=0.2)
        value = response.choices[0].message.content
        if not value or not value.strip():
            raise RuntimeError("llm_enhance_invalid")
        return value.strip()

    def search(self, query: str, top_k: int = 5, api_key: str | None = None, use_llm: bool = False) -> list[tuple[str, float]]:
        """按余弦相似度稳定排序，返回当前 scope 的 meme_id 与匹配度。"""
        if use_llm:
            try:
                query = self._enhance_query(query)
            except Exception:  # noqa: BLE001
                pass
        vector = self._embedding(query)
        with self.resources.environment(self.scope.scope_id) as environment:
            ranked = environment.search.query(self.model, vector, top_k)
            result: list[tuple[str, float]] = []
            seen: set[str] = set()
            for meme_id, score in ranked:
                try:
                    _record, image = self.metadata.image_for_meme(meme_id)
                except MetadataError:
                    continue
                value = str(meme_id)
                if value not in seen:
                    seen.add(value)
                    result.append((value, float(score)))
                if len(result) >= top_k:
                    break
            return result
