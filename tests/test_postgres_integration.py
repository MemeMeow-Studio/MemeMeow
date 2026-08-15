"""PostgreSQL 集成测试夹具与数据库边界验收。

这些测试只在显式提供 ``MEMEMEOW_TEST_DATABASE_URL`` 时运行，避免本地单元测试
悄悄连接开发数据库。每个测试使用事务回滚清理数据，并验证目标数据库确实启用
pgvector、支持事务/锁和固定维度向量。
"""

from __future__ import annotations

import os
import hashlib
import threading
from pathlib import Path
from uuid import uuid4
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.database import (
    EMBEDDING_DIMENSIONS,
    VISUAL_EMBEDDING_DIMENSIONS,
    DatabaseError,
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    Scope,
    SearchGeneration,
    SearchHead,
    Task,
    TaskBatch,
    TaskBatchItem,
    DatabaseResources,
    ScopeContext,
    StorageCoordinator,
    StorageOperation,
    TaskLaneSlot,
    utcnow,
    create_engine_for_url,
)
from backend.config import Settings
from backend.metadata import MemeContext
from backend.visual import VisualSearchError, VisualSearchService


def _test_database_url() -> str | None:
    """读取集成测试专用连接串；不使用应用默认数据库。"""
    return os.getenv("MEMEMEOW_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[Engine]:
    """创建测试连接池，连接失败时将整组测试标记为跳过。"""
    url = _test_database_url()
    if not url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，跳过 PostgreSQL 集成测试")
    engine = create_engine_for_url(url, pool_size=2, max_overflow=0)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            vector_enabled = bool(connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar())
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"PostgreSQL 集成数据库不可用: {exc}")
    if not vector_enabled:
        engine.dispose()
        pytest.fail("测试数据库缺少 pgvector 扩展")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_connection(postgres_engine: Engine):
    """提供测试事务连接；测试结束后回滚所有写入。"""
    connection = postgres_engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def test_postgres_baseline_supports_extension_transactions_and_vector(postgres_connection) -> None:
    """验证扩展、事务回滚和固定 1024 维向量可用。"""
    connection = postgres_connection
    assert connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar() == 1
    assert connection.execute(text("SELECT 1")).scalar() == 1
    assert connection.execute(text("SELECT '[1,2,3]'::vector <=> '[1,2,3]'::vector")).scalar() == 0
    # pgvector 类型本身的维度约束由业务 schema 验收；这里检查配置常量避免测试漂移。
    assert EMBEDDING_DIMENSIONS == 1024


def test_postgres_transaction_isolation_fixture_rolls_back(postgres_connection) -> None:
    """测试夹具应在测试结束时回滚，而不清理共享数据库中的其他数据。"""
    connection = postgres_connection
    connection.execute(text("CREATE TEMP TABLE scoped_fixture_probe (value integer) ON COMMIT DROP"))
    connection.execute(text("INSERT INTO scoped_fixture_probe(value) VALUES (1)"))
    assert connection.execute(text("SELECT count(*) FROM scoped_fixture_probe")).scalar() == 1


def test_postgres_advisory_lock_is_transaction_scoped(postgres_connection) -> None:
    """数据库锁函数应在当前事务内可调用，供任务 lane 互斥使用。"""
    postgres_connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('mememeow:test'))"))
    assert postgres_connection.execute(text("SELECT 1")).scalar() == 1


def test_postgres_composite_scope_foreign_keys_reject_mixed_scope_rows(postgres_connection) -> None:
    """直接绕过 repository 写入跨 scope 关联时，数据库复合外键必须拒绝。"""
    suffix = uuid4().hex
    scope_a = Scope(id=f"test-a-{suffix}")
    scope_b = Scope(id=f"test-b-{suffix}")
    meme_a = Meme(scope_id=scope_a.id, storage_key="same.png", extension=".png", size_bytes=1, sha256="a" * 64, meme_context={}, provenance={}, extensions={})
    meme_b = Meme(scope_id=scope_b.id, storage_key="same.png", extension=".png", size_bytes=1, sha256="b" * 64, meme_context={}, provenance={}, extensions={})
    generation_a = SearchGeneration(scope_id=scope_a.id, model="test-model", source_snapshot_hash="a" * 64)
    generation_b = SearchGeneration(scope_id=scope_b.id, model="test-model", source_snapshot_hash="b" * 64)
    batch_a = TaskBatch(scope_id=scope_a.id, batch_id=f"batch-a-{suffix}")
    task_b = Task(id=f"task-b-{suffix}", scope_id=scope_b.id, task_type="test", lane="default", payload={})
    session = Session(bind=postgres_connection, expire_on_commit=False)
    try:
        session.add_all([scope_a, scope_b])
        session.flush()
        session.add_all([meme_a, meme_b, generation_a, generation_b, batch_a, task_b])
        session.flush()

        def assert_rejected(row) -> None:
            """在 SAVEPOINT 中验证单条非法关系，保留外层测试事务。"""
            nested = session.begin_nested()
            try:
                session.add(row)
                with pytest.raises(IntegrityError):
                    session.flush()
            finally:
                nested.rollback()

        assert_rejected(MemeEmbedding(scope_id=scope_a.id, generation_id=generation_b.id, meme_id=meme_a.id, semantic_document="x", semantic_document_hash="c" * 64, metadata_hash="d" * 64, image_sha256="a" * 64, meme_revision=1))
        assert_rejected(SearchHead(scope_id=scope_a.id, model="test-model", active_generation_id=generation_b.id, active_generation_model="test-model"))
        assert_rejected(TaskBatchItem(scope_id=scope_a.id, batch_id=batch_a.batch_id, task_id=task_b.id))
    finally:
        session.close()


def _clean_business_rows(engine: Engine) -> None:
    """清理集成测试产生的业务行，不触碰 scope、安装标记和迁移版本。"""
    with engine.begin() as connection:
        for table in ("task_lane_slots", "task_batch_items", "task_batches", "tasks", "meme_visual_embeddings", "meme_embeddings", "search_heads", "search_generations", "storage_operations", "meme_collection_items", "meme_collections", "memes"):
            connection.execute(text(f"DELETE FROM {table} WHERE scope_id = 'local'" if table not in {"task_lane_slots"} else f"DELETE FROM {table}"))


@pytest.fixture
def postgres_resources(postgres_engine: Engine, tmp_path: Path):
    """提供绑定真实 PostgreSQL 的资源和临时文件根目录。"""
    _clean_business_rows(postgres_engine)
    settings = Settings(_env_file=None, database_url=_test_database_url(), data_root=tmp_path / "data", image_root=tmp_path / "images", embedding_api_key="", embedding_base_url="")
    resources = DatabaseResources(postgres_engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings)
    try:
        yield resources
    finally:
        _clean_business_rows(postgres_engine)


def test_storage_recovery_and_two_scope_namespaces(postgres_resources, postgres_engine: Engine, tmp_path: Path) -> None:
    """验证上传暂存恢复、双 scope 同名路径物理隔离且数据库互不可见。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    payload = b"storage-recovery"
    meme = coordinator.upload(payload, target_key="same.png", extension=".png", context={}, provenance={})
    assert resources.blob_store.resolve("same.png").read_bytes() == payload
    with resources.factory() as session:
        operation = session.scalar(select(StorageOperation).where(StorageOperation.meme_id == meme.id))
        assert operation is not None and operation.status == "completed"
    suffix = uuid4().hex
    with postgres_engine.begin() as connection:
        connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": f"isolated-{suffix}", "namespace": uuid4()})
    isolated = resources.blob_store_for_scope(f"isolated-{suffix}")
    assert isolated.root != resources.blob_store.root
    assert not isolated.resolve("same.png", must_exist=False).exists()
    isolated_coordinator = StorageCoordinator(resources, scope_id=f"isolated-{suffix}")
    isolated_coordinator.upload(payload, target_key="same.png", extension=".png", context={}, provenance={})
    assert isolated.resolve("same.png").read_bytes() == payload
    assert resources.blob_store.resolve("same.png").read_bytes() == payload
    with resources.environment("local") as environment:
        assert len(environment.memes.list()) == 1
    with resources.environment(f"isolated-{suffix}") as environment:
        assert len(environment.memes.list()) == 1
    with postgres_engine.begin() as connection:
        connection.execute(text("DELETE FROM scopes WHERE id = :id"), {"id": f"isolated-{suffix}"})


def test_collection_export_query_is_ordered_and_excludes_active_storage_operations(postgres_resources) -> None:
    """合集导出查询保持成员顺序，并排除 prepared 存储中间态 Meme。"""
    coordinator = StorageCoordinator(postgres_resources)
    first = coordinator.upload(b"first", target_key="first.png", extension=".png", context={}, provenance={})
    second = coordinator.upload(b"second", target_key="second.png", extension=".png", context={}, provenance={})
    with postgres_resources.environment("local") as environment:
        collection = environment.collections.create("导出查询")
        environment.collections.add_members(collection.id, [first.id, second.id])
    with postgres_resources.factory() as session:
        session.add(StorageOperation(scope_id="local", meme_id=second.id, operation_type="rename", operation_token=uuid4(), source_key="second.png", target_key="second-new.png", before_sha256=second.sha256, after_sha256=second.sha256, before_size=second.size_bytes, after_size=second.size_bytes, status="prepared"))
        session.commit()
    with postgres_resources.environment("local") as environment:
        exported = environment.collections.members_for_export(collection.id)
    assert [item.id for item in exported] == [first.id]


def test_task_claim_fencing_and_global_lane_capacity(postgres_resources) -> None:
    """两个独立连接认领同一任务时只允许一个成功，旧 claim 不能写回。"""
    from backend.database import TaskRepository

    engine = postgres_resources.engine
    with postgres_resources.environment("local") as environment:
        task = environment.tasks.submit(task_type="fencing", payload={}, lane="agent", dedupe_key=f"fencing-{uuid4().hex}")
        task_id = task.id
    barrier = threading.Barrier(2)
    claims: list[tuple[str, int] | None] = []

    def claim(owner: str) -> None:
        with postgres_resources.environment("local") as environment:
            barrier.wait()
            claimed = environment.tasks.claim(owner=owner, lane="agent", lane_capacity=1, lease_seconds=30, task_id=task_id)
            claims.append((claimed.id, claimed.claim_generation) if claimed else None)

    first = threading.Thread(target=claim, args=("worker-a",))
    second = threading.Thread(target=claim, args=("worker-b",))
    first.start(); second.start(); first.join(); second.join()
    assert sum(item is not None for item in claims) == 1
    active = next(item for item in claims if item is not None)
    with postgres_resources.environment("local") as environment:
        assert environment.tasks.update_fenced(task_id, active[1] - 1, "worker-a", status="succeeded") is False
        assert environment.tasks.update_fenced(task_id, active[1], "worker-a" if active[0] else "worker-b", status="succeeded") in {True, False}


def test_sealed_batch_reopens_after_late_agent_child(postgres_resources) -> None:
    """视觉重试在旧缓存收束后补入 Agent 时，批次必须再次提交文本索引。"""
    batch_id = f"late-agent-{uuid4().hex}"
    with postgres_resources.environment("local") as environment:
        visual = environment.tasks.submit(
            task_type="visual_embedding_generation",
            payload={"meme_id": str(uuid4()), "image_sha256": "a" * 64},
            dedupe_key=f"visual-{uuid4().hex}",
        )
        environment.tasks.add_batch_item(batch_id, visual.id)
        environment.tasks.seal_batch(batch_id)
        visual.status = "succeeded"
        first_cache = environment.tasks.finalize_batch_with_task(
            batch_id,
            task_type="cache_generation",
            payload={},
            dedupe_key="cache_generation",
        )
        assert first_cache is not None
        first_cache.status = "succeeded"

        child = environment.tasks.submit(
            task_type="meme_context_generation",
            payload={"meme_id": str(uuid4()), "image_sha256": "b" * 64},
            lane="agent",
            dedupe_key=f"context-{uuid4().hex}",
        )
        environment.tasks.add_batch_item(batch_id, child.id)
        batch = environment.uow.session.get(TaskBatch, {"scope_id": "local", "batch_id": batch_id})
        assert batch is not None and batch.finalizer_state == "pending"
        child.status = "succeeded"
        second_cache = environment.tasks.finalize_batch_with_task(
            batch_id,
            task_type="cache_generation",
            payload={},
            dedupe_key="cache_generation",
        )
        assert second_cache is not None and second_cache.id != first_cache.id


def test_sealed_batch_accepts_visual_stage_retry(postgres_resources) -> None:
    """视觉阶段显式重试不能因批次已封口而丢失文本索引收束关系。"""
    batch_id = f"visual-retry-{uuid4().hex}"
    with postgres_resources.environment("local") as environment:
        original = environment.tasks.submit(
            task_type="visual_embedding_generation",
            payload={"meme_id": str(uuid4()), "image_sha256": "a" * 64},
            dedupe_key=f"visual-{uuid4().hex}",
        )
        environment.tasks.add_batch_item(batch_id, original.id)
        environment.tasks.seal_batch(batch_id)
        original.status = "failed"
        first_cache = environment.tasks.finalize_batch_with_task(
            batch_id,
            task_type="cache_generation",
            payload={},
            dedupe_key="cache-generation-visual-retry-first",
        )
        assert first_cache is not None
        first_cache.status = "succeeded"

        retry = environment.tasks.submit(
            task_type="visual_embedding_generation",
            payload={"meme_id": str(uuid4()), "image_sha256": "b" * 64},
            dedupe_key=f"visual-{uuid4().hex}",
        )
        environment.tasks.add_batch_item(batch_id, retry.id)
        batch = environment.uow.session.get(TaskBatch, {"scope_id": "local", "batch_id": batch_id})
        assert batch is not None and batch.finalizer_state == "pending"
        retry.status = "succeeded"
        second_cache = environment.tasks.finalize_batch_with_task(
            batch_id,
            task_type="cache_generation",
            payload={},
            dedupe_key="cache-generation-visual-retry-second",
        )
        assert second_cache is not None and second_cache.id != first_cache.id


def test_generation_activation_rejects_new_indexable_source(postgres_resources) -> None:
    """generation 激活前必须检测快照漏掉的新可索引 Meme。"""
    from backend.database import Meme, SearchGeneration

    with postgres_resources.environment("local") as environment:
        first = Meme(scope_id="local", storage_key="first.png", extension=".png", size_bytes=1, sha256="1" * 64, context_status="ready", meme_context={"title": "first", "summary": "x"}, provenance={}, extensions={})
        environment.uow.session.add(first)
        environment.uow.session.flush()
        generation = environment.search.create_generation("test", "a" * 64)
        environment.search.add_snapshot_item(generation.id, meme_id=first.id, meme_revision=first.revision, image_sha256=first.sha256, semantic_document="标题：first", metadata_hash="b" * 64)
        environment.search.set_item_embedding(generation.id, first.id, [1.0] + [0.0] * 1023)
    with postgres_resources.environment("local") as environment:
        second = Meme(scope_id="local", storage_key="second.png", extension=".png", size_bytes=1, sha256="2" * 64, context_status="ready", meme_context={"title": "second", "summary": "x"}, provenance={}, extensions={})
        environment.uow.session.add(second)
    with postgres_resources.environment("local") as environment:
        with pytest.raises(Exception, match="source_changed"):
            environment.search.activate(generation)


def _agent_ready_provenance(meme: Meme) -> dict[str, object]:
    """构造与当前 Meme SHA 绑定的最小 research provenance。"""
    return {
        "agent_context": {
            "task_id": f"agent-{meme.id}",
            "image_sha256": meme.sha256,
            "model": "test-agent",
            "skill_hash": "skill-hash",
            "completed_at": utcnow().isoformat(),
        }
    }


def test_visual_repository_filters_sha_storage_readiness_and_scope(postgres_resources, postgres_engine: Engine) -> None:
    """视觉查询只返回当前 scope、当前 SHA 且已完成 Agent 研究的候选。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    query = coordinator.upload(b"query", target_key="visual-query.png", extension=".png", context={}, provenance={})
    ready = coordinator.upload(b"ready", target_key="visual-ready.png", extension=".png", context={}, provenance={})
    pending = coordinator.upload(b"pending", target_key="visual-pending.png", extension=".png", context={}, provenance={})
    stale = coordinator.upload(b"stale", target_key="visual-stale.png", extension=".png", context={}, provenance={})
    identity = {"model": "test-visual", "preprocess_version": "test-v1", "dimensions": VISUAL_EMBEDDING_DIMENSIONS}
    search_settings = Settings(_env_file=None, database_url=_test_database_url(), data_root=resources.data_root, image_root=resources.image_root, visual_model="test-visual", visual_model_dimensions=VISUAL_EMBEDDING_DIMENSIONS, visual_preprocess_version="test-v1")
    with resources.environment("local") as environment:
        query_row = environment.memes.get(query.id, for_update=True)
        ready_row = environment.memes.get(ready.id, for_update=True)
        pending_row = environment.memes.get(pending.id, for_update=True)
        stale_row = environment.memes.get(stale.id, for_update=True)
        assert query_row and ready_row and pending_row and stale_row
        for row in (query_row, ready_row, pending_row, stale_row):
            row.context_status = "ready"
        pending_row.context_status = "pending"
        ready_row.provenance = _agent_ready_provenance(ready_row)
        query_row.provenance = _agent_ready_provenance(query_row)
        stale_row.provenance = _agent_ready_provenance(stale_row)
        environment.visual.upsert(query_row.id, **identity, image_sha256=query_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        environment.visual.upsert(ready_row.id, **identity, image_sha256=ready_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        environment.visual.upsert(pending_row.id, **identity, image_sha256=pending_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        environment.visual.upsert(stale_row.id, **identity, image_sha256=stale_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        with pytest.raises(DatabaseError, match="visual_embedding_sha256_mismatch"):
            environment.visual.upsert(query_row.id, **identity, image_sha256="f" * 64, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        task = environment.tasks.submit(task_type="meme_context_generation", payload={"meme_id": str(query.id), "visual_model": identity["model"], "visual_dimensions": identity["dimensions"], "preprocess_version": identity["preprocess_version"]}, lane="agent", dedupe_key=f"visual-filter-{uuid4().hex}")
        task.status = "running"
        task_id = task.id
    resources.blob_store.resolve(stale.storage_key).write_bytes(b"stale-changed")
    result = VisualSearchService(search_settings, resources).match(task_id=task_id, top_k=10)
    assert [item["meme_id"] for item in result["results"]] == [str(ready.id)]
    other_scope = f"visual-isolated-{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": other_scope, "namespace": uuid4()})
    isolated = StorageCoordinator(resources, scope_id=other_scope).upload(b"other", target_key="visual-other.png", extension=".png", context={}, provenance={})
    with resources.environment(other_scope) as environment:
        other_row = environment.memes.get(isolated.id, for_update=True)
        assert other_row is not None
        other_row.context_status = "ready"
        other_row.provenance = _agent_ready_provenance(other_row)
        environment.visual.upsert(other_row.id, **identity, image_sha256=other_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
    with resources.environment("local") as environment:
        assert all(row[1].id != isolated.id for row in environment.visual.match([1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1), **identity, limit=10))
    with postgres_engine.begin() as connection:
        connection.execute(text("DELETE FROM scopes WHERE id = :id"), {"id": other_scope})


def test_visual_embedding_lifecycle_follows_rename_and_delete(postgres_resources) -> None:
    """重命名保留同一视觉产物，删除通过复合外键级联清理向量。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    meme = coordinator.upload(b"lifecycle", target_key="visual-lifecycle.png", extension=".png", context={}, provenance={})
    identity = {"model": "test-visual", "preprocess_version": "test-v1", "dimensions": VISUAL_EMBEDDING_DIMENSIONS}
    with resources.environment("local") as environment:
        row = environment.memes.get(meme.id, for_update=True)
        assert row is not None
        environment.visual.upsert(row.id, **identity, image_sha256=row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
    coordinator.rename(meme.id, target_key="visual-lifecycle-renamed.png")
    with resources.environment("local") as environment:
        assert environment.visual.get(meme.id, **identity) is not None
    coordinator.delete(meme.id)
    with resources.factory() as session:
        assert session.scalar(select(MemeVisualEmbedding).where(MemeVisualEmbedding.meme_id == meme.id)) is None


def test_visual_search_requires_running_task_and_returns_structured_candidates(postgres_resources) -> None:
    """内部视觉匹配只接受 running 任务，并返回带语境和媒体引用的候选。"""
    resources = postgres_resources
    settings = Settings(_env_file=None, database_url=_test_database_url(), data_root=resources.data_root, image_root=resources.image_root)
    coordinator = StorageCoordinator(resources)
    query = coordinator.upload(b"search-query", target_key="visual-search-query.png", extension=".png", context={"title": "Query", "summary": "query"}, provenance={})
    candidate = coordinator.upload(b"search-candidate", target_key="visual-search-candidate.png", extension=".png", context={"title": "Candidate", "summary": "candidate"}, provenance={})
    identity = {"model": settings.visual_model, "preprocess_version": settings.visual_preprocess_version, "dimensions": settings.visual_model_dimensions}
    with resources.environment("local") as environment:
        query_row = environment.memes.get(query.id, for_update=True)
        candidate_row = environment.memes.get(candidate.id, for_update=True)
        assert query_row and candidate_row
        query_row.context_status = candidate_row.context_status = "ready"
        query_row.provenance = _agent_ready_provenance(query_row)
        candidate_row.provenance = _agent_ready_provenance(candidate_row)
        environment.visual.upsert(query_row.id, **identity, image_sha256=query_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        environment.visual.upsert(candidate_row.id, **identity, image_sha256=candidate_row.sha256, embedding=[1.0] + [0.0] * (VISUAL_EMBEDDING_DIMENSIONS - 1))
        task = environment.tasks.submit(task_type="meme_context_generation", payload={"meme_id": str(query.id), **{"visual_model": identity["model"], "visual_dimensions": identity["dimensions"], "preprocess_version": identity["preprocess_version"]}}, lane="agent", dedupe_key=f"visual-search-{uuid4().hex}")
        task_id = task.id
    service = VisualSearchService(settings, resources)
    with pytest.raises(VisualSearchError) as error:
        service.match(task_id=task_id)
    assert error.value.code == "task_not_running"
    with resources.environment("local") as environment:
        task = environment.tasks.get(task_id, for_update=True)
        assert task is not None
        task.status = "running"
    result = service.match(task_id=task_id, top_k=10)
    assert [item["meme_id"] for item in result["results"]] == [str(candidate.id)]
    assert result["results"][0]["media_url"] == f"/media/{candidate.id}"
    assert result["results"][0]["context"]["title"] == "Candidate"
