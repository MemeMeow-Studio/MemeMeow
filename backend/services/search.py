from __future__ import annotations

import logging
from typing import Any

import numpy as np
from openai import OpenAI
from sqlalchemy import select

from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    ImageProcessingAttempt,
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    ScopeContext,
    SearchGeneration,
    StorageOperation,
    Task,
    TaskBatch,
    TaskLaneSlot,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.metadata import (
    EMBEDDING_FIELDS,
    MAX_SEMANTIC_DOCUMENT_LENGTH,
    MetadataError,
    MemeContext,
    semantic_document,
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
            if environment.search.source_mode(self.model) == "incremental":
                return environment.search.has_incremental(self.model)
            # active generation 只说明控制面存在一代索引，不能证明其中仍有
            # 与当前 Meme SHA、语境 hash、维度和文件身份一致的可检索条目。
            return environment.search.has_legacy(self.model)

    def invalidate_cache(self) -> None:
        """数据库索引不在进程内缓存；此方法保留兼容调用但无副作用。"""

    def mark_cache_invalidated(self, batch_id: object = None) -> None:
        """兼容旧批次回调，generation 由独立任务显式刷新。"""

    def generate_cache(self, progress: Callable[[float | None, str | None], None], claim: tuple[str, int, str] | None = None) -> dict[str, object]:
        """短事务固化源快照，在事务外调用 embedding，完成后原子激活。"""
        with self._generation_lock:
            candidates: list[tuple[UUID, int, str, str, str]] = []
            skipped = 0
            with self.resources.environment(self.scope.scope_id) as environment:
                # 源集合必须在同一个短 REPEATABLE READ 事务中固化，避免刷新期间
                # 读取到跨事务的 Meme revision/语境组合。
                environment.uow.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                environment.search.assert_claim(claim)
                # 同一 scope/model 的 building generation 可能来自上次 Worker 崩溃；
                # 新 claim 先将其隔离，再创建本次不可变 generation。
                environment.search.abandon_building(self.model, claim=claim)
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
                    serialized = json.dumps(self.metadata._to_sidecar(meme).model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    metadata_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                    candidates.append((meme.id, meme.revision, meme.sha256, text_value, metadata_hash))
                if not candidates:
                    raise RuntimeError("no_indexable_images")
                candidates.sort(key=lambda item: str(item[0]))
                source = [(str(meme_id), revision, image_sha, metadata_hash, hashlib.sha256(text.encode()).hexdigest()) for meme_id, revision, image_sha, text, metadata_hash in candidates]
                source_hash = hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                generation = environment.search.create_generation(self.model, source_hash)
                generation_id = generation.id
                for meme_id, revision, image_sha, text_value, metadata_hash in candidates:
                    environment.search.assert_claim(claim)
                    environment.search.add_snapshot_item(generation_id, meme_id=meme_id, meme_revision=revision, image_sha256=image_sha, semantic_document=text_value[:MAX_SEMANTIC_DOCUMENT_LENGTH], metadata_hash=metadata_hash)
            total = len(candidates)
            try:
                for index, (meme_id, _revision, _image_sha, text_value, _metadata_hash) in enumerate(candidates, start=1):
                    vector = self._embedding(text_value)
                    with self.resources.environment(self.scope.scope_id) as environment:
                        environment.search.set_item_embedding(generation_id, meme_id, vector, claim=claim)
                    if progress:
                        progress(index / total, f"正在生成 pgvector 索引 {index}/{total}")
                with self.resources.environment(self.scope.scope_id) as environment:
                    environment.search.activate(generation, expected_source_hash=source_hash, expected_items=source, claim=claim)
                return {"indexed_count": total, "skipped_count": skipped, "generation_id": str(generation_id), "model": self.model, "dimensions": EMBEDDING_DIMENSIONS}
            except Exception as exc:
                with self.resources.environment(self.scope.scope_id) as environment:
                    try:
                        environment.search.fail_generation(generation_id, error=str(exc)[:500], claim=claim)
                    except DatabaseError:
                        # 租约已被新 Worker 接管时，旧 Worker 不得修改 generation 状态。
                        pass
                raise

    def _enhance_query(self, query: str) -> str:
        """使用可选 LLM 改写查询；失败由 API 回退普通查询。"""
        if not self.settings.llm_enhance_model:
            raise RuntimeError("llm_enhance_not_configured")
        response = self._client().chat.completions.create(model=self.settings.llm_enhance_model, messages=[{"role": "user", "content": f"将下面内容改写为一句适合检索表情包的简短描述，只输出描述：{query}"}], temperature=0.2)
        value = response.choices[0].message.content
        if not value or not value.strip():
            raise RuntimeError("llm_enhance_invalid")
        return value.strip()

    def search(self, query: str, top_k: int = 5, api_key: str | None = None, use_llm: bool = False) -> list[str]:
        """按 meme_id 稳定排序查询并返回当前 scope 的稳定资源标识。"""
        if use_llm:
            try:
                query = self._enhance_query(query)
            except Exception:  # noqa: BLE001
                pass
        vector = self._embedding(query)
        with self.resources.environment(self.scope.scope_id) as environment:
            if environment.search.source_mode(self.model) == "incremental":
                ranked = environment.search.query_incremental(self.model, vector, top_k)
            else:
                ranked = environment.search.query(self.model, vector, top_k)
            result: list[str] = []
            for meme_id, _score in ranked:
                try:
                    _record, image = self.metadata.image_for_meme(meme_id)
                except MetadataError:
                    continue
                value = str(meme_id)
                if value not in result:
                    result.append(value)
                if len(result) >= top_k:
                    break
            return result
