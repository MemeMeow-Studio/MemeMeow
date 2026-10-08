from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import numpy as np
from openai import DefaultHttpxClient, OpenAI
from sqlalchemy import select

from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    Meme,
    ScopeContext,
    Task,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.metadata import MetadataError
from backend.services.metadata import PostgresMetadataService
from backend.embedding_input import build_text_input
from backend.text_updates import enqueue_text_update
from runtime_telemetry import instrument_httpx_client, observed_operation, record_content

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
        return OpenAI(api_key=self.settings.embedding_api_key, base_url=self.settings.embedding_base_url, http_client=instrument_httpx_client(DefaultHttpxClient()))

    def _embedding(self, text: str) -> list[float]:
        """调用模型并拒绝非 1024 维或零向量响应。"""
        with observed_operation("embedding", unit="python_provider_call", attributes={"provider": "openai_compatible", "model": self.model, "scope.id": self.scope.scope_id}) as span:
            record_content(span, "provider.input", {"input": text})
            response = self._client().embeddings.create(model=self.model, input=text, encoding_format="float")
            record_content(span, "provider.response", {"model": response.model, "usage": response.usage.model_dump() if response.usage else None, "dimension": len(response.data[0].embedding)})
        vector = np.asarray(response.data[0].embedding, dtype=float)
        if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.linalg.norm(vector):
            raise RuntimeError("embedding_dimensions_mismatch")
        return (vector / np.linalg.norm(vector)).tolist()

    def has_cache(self) -> bool:
        """检查当前 scope/model 的唯一检索来源是否已有有效数据。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.search.has_incremental(self.model)

    def invalidate_cache(self) -> None:
        """数据库索引不在进程内缓存；此方法保留兼容调用但无副作用。"""

    def mark_cache_invalidated(self, batch_id: object = None) -> None:
        """兼容旧批次回调，generation 由独立任务显式刷新。"""

    def valid_text_embedding_ids(self, memes: Sequence[Meme]) -> set[UUID]:
        """返回给定图片中通过当前 scope/model 全部指纹校验的文本向量 ID。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.search.valid_text_embedding_ids(self.model, memes)

    def generate_cache(self, progress: Callable[[float | None, str | None], None], claim: tuple[str, int, str] | None = None) -> dict[str, object]:
        """提交去重的单图任务，周期读取其终态并汇总部分成功和错误。"""
        if claim is None:
            raise DatabaseError("cache_generation_claim_required")
        with self._generation_lock:
            with self.resources.environment(self.scope) as environment:
                environment.search.assert_claim(claim)
                parent = environment.tasks.get(claim[0])
                plan = parent.payload.get("cache_plan")
            if plan is not None:
                return self._cache_result(plan, claim)
            preflight = self.resources.search_rebuild_preflight(self.scope)
            if preflight["blocking"]:
                logger.warning("search_rebuild_blocked scope=%s keys=%s", self.scope.scope_id, preflight["blocking_keys"])
                raise RuntimeError("search_rebuild_preflight_failed")
            counts = {"reused_count": 0, "active_count": 0, "waiting_for_text_count": 0, "needed_count": 0}
            children: list[str] = []
            errors: list[dict[str, str]] = []
            after_id: UUID | None = None
            while True:
                with self.resources.environment(self.scope.scope_id) as environment:
                    environment.search.assert_claim(claim)
                    page = environment.memes.list_rebuild_page(after_id=after_id, limit=200)
                if not page:
                    break
                for snapshot in page:
                    # 单图任务负责文件身份检查和模型执行，扫描仅保存当前输入。
                    with self.resources.environment(self.scope) as environment:
                        environment.search.assert_claim(claim)
                        current = environment.memes.get(snapshot.id, for_update=True)
                        if current is None:
                            errors.append({"meme_id": str(snapshot.id), "error": "meme_not_found"})
                            continue
                        text_input = build_text_input(current.display_name, current.meme_context, current.context_status)
                        current.search_metadata_hash = text_input.fingerprint
                        current.text_input_status = text_input.status
                        if text_input.status == "waiting_for_text":
                            counts["waiting_for_text_count"] += 1
                            continue
                        if text_input.status != "eligible":
                            errors.append({"meme_id": str(current.id), "error": text_input.status})
                            continue
                        if current.id in environment.search.valid_text_embedding_ids(self.model, [current]):
                            counts["reused_count"] += 1
                            continue
                        active = environment.uow.session.scalar(select(Task.id).where(
                            Task.scope_id == self.scope.scope_id,
                            Task.target_meme_id == current.id,
                            Task.target_image_sha256 == current.sha256,
                            Task.task_type == "text_embedding_generation",
                            Task.status.in_(("queued", "running")),
                            Task.payload["metadata_hash"].astext == text_input.fingerprint,
                            Task.payload["embedding_model"].astext == self.model,
                        ).limit(1))
                        if active is not None:
                            counts["active_count"] += 1
                            children.append(active)
                            continue
                        child = enqueue_text_update(environment.uow.session, current)
                        if child is not None:
                            counts["needed_count"] += 1
                            children.append(child.id)
                    if progress:
                        progress(None, f"已提交 {counts['needed_count']} 张图片的文本修复")
                after_id = page[-1].id
            plan = {"counts": counts, "task_ids": children, "errors": errors}
            with self.resources.environment(self.scope) as environment:
                if not environment.tasks.update_payload_fenced(*claim, {"cache_plan": plan}):
                    raise DatabaseError("claim_expired")
            return self._cache_result(plan, claim)

    def _cache_result(self, plan: dict[str, Any], claim: tuple[str, int, str]) -> dict[str, object]:
        """从持久子任务汇总当前结果；活动子任务让父任务归还执行线程。"""
        with self.resources.environment(self.scope) as environment:
            environment.search.assert_claim(claim)
            children = list(environment.uow.session.scalars(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id.in_(plan["task_ids"]))))
        counts = dict(plan["counts"])
        errors = list(plan["errors"])
        errors.extend({"task_id": child.id, "error": child.error} for child in children if child.status == "failed")
        missing = set(plan["task_ids"]) - {child.id for child in children}
        errors.extend({"task_id": task_id, "error": "task_not_found"} for task_id in missing)
        succeeded = sum(child.status == "succeeded" for child in children)
        return {**counts, "generated_count": succeeded, "failed_count": len(errors), "errors": errors,
            "indexed_count": counts["reused_count"] + succeeded, "model": self.model,
            "dimensions": EMBEDDING_DIMENSIONS, "_pending": any(child.status in {"queued", "running"} for child in children)}

    def _enhance_query(self, query: str) -> str:
        """使用可选 LLM 改写查询；失败由 API 回退普通查询。"""
        if not self.settings.llm_enhance_model:
            raise RuntimeError("llm_enhance_not_configured")
        with observed_operation("query_enhancement", unit="python_provider_call", attributes={"provider": "openai_compatible", "model": self.settings.llm_enhance_model, "scope.id": self.scope.scope_id}) as span:
            record_content(span, "provider.input", {"query": query})
            response = self._client().chat.completions.create(model=self.settings.llm_enhance_model, messages=[{"role": "user", "content": f"将下面内容改写为一句适合检索表情包的简短描述，只输出描述：{query}"}], temperature=0.2)
            record_content(span, "provider.response", response.model_dump())
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
        # 图片校验会读取文件并查询当前记录，查询事务应在进入校验前释放连接。
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
