"""PostgreSQL 模型、scope 绑定和固定向量维度的静态契约测试。"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from backend.database import Base, EMBEDDING_DIMENSIONS, VISUAL_EMBEDDING_DIMENSIONS, BlobStore, DatabaseError, ScopeContext, SearchRepository
from backend.paths import validate_business_storage_key
from api import image_metadata


def test_schema_contains_scope_and_queue_tables():
    """首版 schema 必须覆盖业务记录、generation 和持久任务队列。"""
    expected = {"scopes", "installation_state", "memes", "storage_operations", "derived_image_thumbnails", "search_generations", "search_heads", "meme_embeddings", "meme_visual_embeddings", "meme_text_embeddings", "search_migration_states", "image_processing_jobs", "image_processing_stages", "image_processing_attempts", "operation_grants", "agent_callback_requests", "tasks", "task_batches", "task_batch_items", "task_lane_slots", "meme_collections", "meme_collection_items", "reverse_image_usage_events"}
    assert expected <= set(Base.metadata.tables)
    assert EMBEDDING_DIMENSIONS == 1024
    assert Base.metadata.tables["meme_embeddings"].c.embedding.type.dim == 1024
    assert VISUAL_EMBEDDING_DIMENSIONS == 768
    assert Base.metadata.tables["meme_visual_embeddings"].c.embedding.type.dim == 768


def test_search_query_dispatches_to_one_migration_source(monkeypatch):
    """SearchRepository.query 必须遵循迁移来源选择，不能混读两套向量。"""
    repository = object.__new__(SearchRepository)
    vector = [1.0] * EMBEDDING_DIMENSIONS
    monkeypatch.setattr(repository, "source_mode", lambda _model: "incremental")
    monkeypatch.setattr(repository, "query_incremental", lambda _model, _vector, _limit: [(uuid4(), 1.0)])
    monkeypatch.setattr(repository, "_query_legacy_validated", lambda *_args: pytest.fail("不应查询旧 generation"))
    assert len(SearchRepository.query(repository, "model", vector)) == 1

    monkeypatch.setattr(repository, "source_mode", lambda _model: "legacy")
    monkeypatch.setattr(repository, "_query_legacy_validated", lambda _model, _vector, _limit: [(uuid4(), 0.5)])
    monkeypatch.setattr(repository, "query_incremental", lambda *_args: pytest.fail("不应查询增量向量"))
    assert len(SearchRepository.query(repository, "model", vector)) == 1


def test_scope_context_rejects_empty_scope():
    """所有数据访问入口都必须有不可为空 scope。"""
    with pytest.raises(ValueError, match="scope_required"):
        ScopeContext("")


def test_business_storage_key_is_flat_but_internal_key_is_separate():
    """业务校验拒绝嵌套和内部保留名，同时不影响 BlobStore 内部命名空间。"""
    for value in ("nested/meme.png", "..", ".staging", "bad\nname.png"):
        with pytest.raises(ValueError):
            validate_business_storage_key(value)
    assert validate_business_storage_key("meme.png") == "meme.png"


def test_blob_store_rejects_scope_prefix_and_symlink(tmp_path):
    """BlobStore 不接受绝对路径、穿越或符号链接逃逸。"""
    store = BlobStore(root=tmp_path, scope=ScopeContext("local"), local=True)
    with pytest.raises(DatabaseError):
        store.resolve("../secret.png", must_exist=False)
    with pytest.raises(DatabaseError):
        store.resolve("/tmp/secret.png", must_exist=False)
    outside = tmp_path.parent / f"outside-{uuid4().hex}"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DatabaseError, match="symlink"):
        store.resolve("escape/file.png", must_exist=False)


def test_metadata_http_contract_only_exposes_stable_meme_id():
    """元数据入口的公开签名不得继续暴露旧 directory/filename 资源标识。"""
    parameters = inspect.signature(image_metadata).parameters
    assert "meme_id" in parameters
    assert "directory" not in parameters
    assert "filename" not in parameters
