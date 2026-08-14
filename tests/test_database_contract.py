"""PostgreSQL 模型、scope 绑定和固定向量维度的静态契约测试。"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from backend.database import Base, CollectionRepository, EMBEDDING_DIMENSIONS, BlobStore, DatabaseError, ScopeContext
from backend.paths import validate_business_storage_key
from api import image_metadata


def test_schema_contains_scope_and_queue_tables():
    """首版 schema 必须覆盖业务记录、generation 和持久任务队列。"""
    expected = {"scopes", "installation_state", "memes", "storage_operations", "search_generations", "search_heads", "meme_embeddings", "tasks", "task_batches", "task_batch_items", "task_lane_slots", "meme_collections", "meme_collection_items"}
    assert expected <= set(Base.metadata.tables)
    assert EMBEDDING_DIMENSIONS == 1024
    assert Base.metadata.tables["meme_embeddings"].c.embedding.type.dim == 1024


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


def test_collection_repository_is_scope_bound():
    """合集 repository 的公开方法不接受客户端 scope 覆盖参数。"""
    public = {name for name in dir(CollectionRepository) if not name.startswith("_") and callable(getattr(CollectionRepository, name))}
    for name in public:
        assert "scope_id" not in inspect.signature(getattr(CollectionRepository, name)).parameters
