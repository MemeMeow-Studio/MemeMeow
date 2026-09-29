# 在图片元数据事务中持久提交文本后续任务，供名称、语境和修复入口共用。

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.embedding_input import build_text_input
from backend.persistence.engine import DatabaseError
from backend.persistence.models import EMBEDDING_DIMENSIONS, Meme, MemeTextEmbedding, ScopeContext, Task


def enqueue_text_update(session: Session, meme: Meme, *, source_task: Task | None = None) -> Task | None:
    """复用当前输入的向量或活动任务，必要时在已有 Session 创建独立文本任务。"""
    from backend.persistence.repositories.tasks import TaskRepository

    text_input = build_text_input(meme.display_name, meme.meme_context, meme.context_status)
    meme.search_metadata_hash = text_input.fingerprint
    meme.text_input_status = text_input.status
    if text_input.status != "eligible":
        return None
    # 完整流程的父 Job 已持久保存文本计划，在标题命名结束后使用最终输入。
    if source_task is not None and source_task.submission_mode == "pipeline":
        return None
    model = session.info.get("embedding_model")
    if not isinstance(model, str) or not model:
        raise DatabaseError("embedding_model_not_configured")
    existing = session.scalar(select(MemeTextEmbedding).where(
        MemeTextEmbedding.scope_id == meme.scope_id,
        MemeTextEmbedding.meme_id == meme.id,
        MemeTextEmbedding.image_sha256 == meme.sha256,
        MemeTextEmbedding.metadata_hash == text_input.fingerprint,
        MemeTextEmbedding.embedding_model_version == model,
        MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
        MemeTextEmbedding.status == "ready",
        MemeTextEmbedding.embedding.is_not(None),
    ))
    if existing is not None:
        return None
    active = session.scalar(select(Task).where(
        Task.scope_id == meme.scope_id, Task.target_meme_id == meme.id,
        Task.target_image_sha256 == meme.sha256, Task.task_type == "text_embedding_generation",
        Task.status.in_(("queued", "running")),
        Task.payload["metadata_hash"].astext == text_input.fingerprint,
        Task.payload["embedding_model"].astext == model,
    ).limit(1))
    if active is not None:
        return active
    return TaskRepository(session, ScopeContext(meme.scope_id)).submit(
        task_type="text_embedding_generation",
        lane="text_embedding",
        submission_mode="standalone",
        image_stage="text_embedding",
        target_meme_id=meme.id,
        target_image_sha256=meme.sha256,
        max_attempts=1,
        dedupe_key=f"text-input:{meme.id}:{meme.sha256}:{text_input.fingerprint}:{model}",
        payload={
            "submission_mode": "standalone", "stage": "text_embedding",
            "meme_id": str(meme.id), "image_sha256": meme.sha256,
            "metadata_hash": text_input.fingerprint, "embedding_model": model,
            "text_followup": True,
            "source_task_id": source_task.id if source_task else None,
        },
    )
