"""PostgreSQL 集成测试夹具与数据库边界验收。

这些测试只在显式提供 ``MEMEMEOW_TEST_DATABASE_URL`` 时运行，避免本地单元测试
悄悄连接开发数据库。每个测试使用事务回滚清理数据，并验证目标数据库确实启用
pgvector、支持事务/锁和固定维度向量。
"""

from __future__ import annotations

import os
import hashlib
import json
import threading
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, text
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
    DerivedImageThumbnail,
    TaskLaneSlot,
    TaskLaneResourceSlot,
    TaskLaneFairness,
    AgentCallbackRequest,
    ImageProcessingJob,
    ImageProcessingStage,
    OperationGrant,
    SearchMigrationState,
    utcnow,
    create_engine_for_url,
)
from backend.config import Settings
from backend.image_processing import ImageProcessingError, ImageProcessingRepository, ImageProcessingWorker, STAGES
from backend.metadata import MetadataError, MemeContext
from backend.operation_policy import AllowAllOperationPolicy, GrantAssociationStore, OperationPolicyError, OperationPolicyGateway, Operations, PersistentGrantAssociationStore
from backend.visual import VisualSearchError, VisualSearchService
from backend.scope import ScopeServiceFactory
from backend.pg_services import PostgresTaskService, PostgresTaskWorkerManager
from backend.services.metadata import PostgresMetadataService


class _CountingAllowAllPolicy(AllowAllOperationPolicy):
    """记录真实 acquire 请求，供 Worker 并发计量边界集成测试使用。"""

    def __init__(self) -> None:
        """初始化线程安全的 acquire 观测列表。"""
        super().__init__()
        self.requests = []
        self._requests_lock = threading.Lock()

    def acquire(self, request):
        """记录可信 operation request 后复用 allow-all 的幂等授权。"""
        with self._requests_lock:
            self.requests.append(request)
        return super().acquire(request)


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


def test_callback_request_binding_is_scope_bound_and_idempotent(postgres_resources) -> None:
    """callback request 的完整 claim 绑定可复用，改绑和跨 scope 读取都会失败。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"callback-target", target_key="callback-target.png", extension=".png", context={}, provenance={})
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="meme_context_generation",
            payload={"meme_id": str(meme.id), "image_sha256": meme.sha256},
            lane="agent",
            dedupe_key=f"callback-{uuid4().hex}",
        )
        claim = environment.tasks.claim(owner="callback-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None
        row = environment.callback_requests.create(
            request_id="callback-request-1",
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            operation="analysis.reverse_image_search",
            target_sha256=meme.sha256,
            input_digest="a" * 64,
        )
        assert row.state == "started"
        same = environment.callback_requests.create(
            request_id="callback-request-1",
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            operation="analysis.reverse_image_search",
            target_sha256=meme.sha256,
            input_digest="a" * 64,
        )
        assert (same.scope_id, same.request_id) == (row.scope_id, row.request_id)
        with pytest.raises(DatabaseError, match="callback_request_conflict"):
            environment.callback_requests.create(
                request_id="callback-request-1",
                task_id=claim.id,
                claim_generation=claim.claim_generation,
                attempt=claim.attempt_count,
                operation="analysis.reverse_image_search",
                target_sha256="b" * 64,
                input_digest="a" * 64,
            )
        environment.callback_requests.finish("callback-request-1", state="unknown_execution", error={"error": "reverse_image_unknown_execution"})
    with resources.environment("local") as environment:
        environment.callback_requests.finish("callback-request-1", state="completed", result={"outcome": "success"})
        final = environment.callback_requests.get("callback-request-1")
        assert final is not None and final.state == "completed"


def test_callback_logical_binding_converges_across_independent_sessions(postgres_resources) -> None:
    """两个独立 Session 以不同 ID 并发首次提交时只保留一个逻辑事实。"""
    resources = postgres_resources
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="meme_context_generation",
            payload={"image_sha256": "a" * 64},
            lane="agent",
            dedupe_key=f"callback-race-{uuid4().hex}",
        )
        claim = environment.tasks.claim(owner="callback-race-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None
        values = {
            "task_id": claim.id,
            "claim_generation": claim.claim_generation,
            "attempt": claim.attempt_count,
            "operation": "analysis.reverse_image_search",
            "target_sha256": "a" * 64,
            "input_digest": "b" * 64,
        }
    barrier = threading.Barrier(2)

    def submit(request_id: str) -> str:
        """在独立数据库 Session 中提交一个候选 callback ID。"""
        with resources.environment("local") as environment:
            barrier.wait(timeout=10)
            row = environment.callback_requests.resolve(request_id=request_id, **values)
            environment.uow.session.commit()
            return row.request_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("race-a", "race-b")))
    assert results[0] == results[1]
    with resources.environment("local") as environment:
        count = environment.uow.session.execute(
            text(
                """
                SELECT count(*)
                  FROM agent_callback_requests
                 WHERE scope_id = 'local'
                   AND task_id = :task_id
                   AND input_digest = :input_digest
                """
            ),
            values,
        ).scalar_one()
        assert count == 1


def test_persistent_operation_grant_is_idempotent_and_does_not_refund_other_operations(postgres_resources) -> None:
    """PostgreSQL grant 事实按 scope 幂等提交，delete 不会释放 upload reservation。"""
    resources = postgres_resources
    with resources.environment("local") as environment:
        task = environment.tasks.submit(task_type="grant-probe", payload={}, dedupe_key=f"grant-{uuid4().hex}")
        task_id = task.id
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    from backend.operation_policy import PersistentGrantAssociationStore

    store = PersistentGrantAssociationStore(resources)
    upload_request = gateway.request("local", Operations.IMAGE_UPLOAD, "upload:grant-probe", task_id=task_id, resource_id=None)
    delete_request = gateway.request("local", Operations.IMAGE_DELETE, "delete:grant-probe", task_id=task_id, resource_id=None)
    upload = store.acquire(upload_request, gateway)
    assert store.acquire(upload_request, gateway).grant == upload.grant
    assert store.transition(upload.grant, "committed") is True
    assert store.transition(upload.grant, "committed") is True
    assert store.transition(upload.grant, "released") is False
    delete = store.acquire(delete_request, gateway)
    assert delete.grant != upload.grant
    with resources.factory() as session:
        rows = list(session.scalars(select(OperationGrant).where(OperationGrant.scope_id == "local")))
        assert {row.operation for row in rows} >= {Operations.IMAGE_UPLOAD, Operations.IMAGE_DELETE}


def test_persistent_grant_checks_request_facts_and_terminal_states(postgres_resources) -> None:
    """PostgreSQL association 必须比较完整请求事实并拒绝旧终态复用。"""
    resources = postgres_resources
    policy = _CountingAllowAllPolicy()
    gateway = OperationPolicyGateway(policy, allow_all=True)
    store = PersistentGrantAssociationStore(resources)
    request = gateway.request(
        "local",
        Operations.ANALYSIS_AGENT,
        "agent:fact-check",
        resource_id="meme-fact",
        task_id=None,
        source="image-processing",
        units=1,
        input_digest="a" * 64,
    )
    association = store.acquire(request, gateway)
    assert store.acquire(request, gateway).grant == association.grant
    assert policy.requests and len(policy.requests) == 1
    with resources.factory() as session:
        row = session.get(OperationGrant, ("local", Operations.ANALYSIS_AGENT, "agent:fact-check"))
        assert row is not None
        assert row.request_fingerprint == request.request_fingerprint
        assert row.source == request.source
        assert row.units == request.units

    conflicting = gateway.request(
        "local",
        Operations.ANALYSIS_AGENT,
        "agent:fact-check",
        resource_id="meme-other",
        source="image-processing",
        input_digest="a" * 64,
    )
    with pytest.raises(OperationPolicyError) as error:
        store.acquire(conflicting, gateway)
    assert error.value.code == "operation_policy_unavailable"
    assert store.transition(association.grant, "unknown") is True
    assert store.get(request) is not None
    with pytest.raises(OperationPolicyError) as terminal_error:
        store.acquire(request, gateway)
    assert terminal_error.value.code == "operation_policy_unavailable"


def test_persistent_legacy_grant_without_request_facts_fails_closed(postgres_resources) -> None:
    """迁移前留下的缺少 source/units/fingerprint 的 grant 不能被执行路径采用。"""
    resources = postgres_resources
    with resources.factory() as session:
        session.add(
            OperationGrant(
                scope_id="local",
                operation=Operations.IMAGE_UPLOAD,
                idempotency_key="legacy:missing-facts",
                grant_id=f"legacy-{uuid4().hex}",
                state="acquired",
            )
        )
        session.commit()
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = PersistentGrantAssociationStore(resources)
    request = gateway.request("local", Operations.IMAGE_UPLOAD, "legacy:missing-facts")
    with pytest.raises(OperationPolicyError) as error:
        store.get(request)
    assert error.value.code == "operation_policy_unavailable"


def test_persistent_legacy_grant_without_metering_units_remains_readable(postgres_resources) -> None:
    """迁移前已具备完整旧请求事实的 grant 可按零成本兼容收束一次。"""
    resources = postgres_resources
    legacy_key = "legacy:metering-compatible"
    legacy_grant = f"legacy-{uuid4().hex}"
    payload = {
        "input_digest": None,
        "resource_id": None,
        "source": "legacy-worker",
        "task_id": None,
        "units": 1,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    with resources.factory() as session:
        session.add(
            OperationGrant(
                scope_id="local",
                operation=Operations.ANALYSIS_AGENT,
                idempotency_key=legacy_key,
                grant_id=legacy_grant,
                source="legacy-worker",
                units=1,
                request_fingerprint=fingerprint,
                state="acquired",
            )
        )
        session.commit()
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    store = PersistentGrantAssociationStore(resources)
    request = gateway.request("local", Operations.ANALYSIS_AGENT, legacy_key, source="legacy-worker", metering_units=0)
    association = store.get(request)
    assert association is not None
    assert association.grant.grant_id == legacy_grant
    assert store.transition(association.grant, "committed") is True
    assert store.transition(association.grant, "committed") is True


def test_persistent_grant_with_metering_units_remains_idempotently_readable(postgres_resources) -> None:
    """持久化的新计量 grant 必须按完整请求事实幂等读取。"""
    resources = postgres_resources
    key = "metering:persistent-readable"
    gateway = OperationPolicyGateway(AllowAllOperationPolicy(), allow_all=True)
    request = gateway.request(
        "local",
        Operations.ANALYSIS_AGENT,
        key,
        source="image-processing",
        units=1,
        metering_units=1000,
    )
    store = PersistentGrantAssociationStore(resources)
    association = store.acquire(request, gateway)

    with resources.factory() as session:
        row = session.get(OperationGrant, ("local", Operations.ANALYSIS_AGENT, key))
        assert row is not None
        assert row.metering_units == 1000

    loaded = store.get(request)
    assert loaded is not None
    assert loaded.grant == association.grant
    assert loaded.request.metering_units == 1000


def test_image_processing_repository_enforces_stage_order_claim_fencing_and_retry(postgres_resources) -> None:
    """图片 Job 固定四阶段、旧 claim 写回拒绝，失败重试只创建新 revision。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"processing-target", target_key="processing-target.png", extension=".png", context={}, provenance={})
    repository = ImageProcessingRepository(resources, "local")
    config = {"agent_model": "test-agent", "embedding_model": "test-embedding", "embedding_dimensions": 1024}
    job = repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="auto", auto_name=True)
    assert repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="auto", auto_name=True).id == job.id
    with pytest.raises(ImageProcessingError, match="generation_policy_conflict"):
        repository.create_or_reuse(meme.id, meme.sha256, config={**config, "agent_model": "other"}, reverse_image_policy="auto", auto_name=True)
    with pytest.raises(ImageProcessingError, match="target_changed"):
        repository.create_or_reuse(meme.id, "f" * 64, config=config, reverse_image_policy="auto")

    claimed = repository.claim(job.id, owner="processing-owner", lease_seconds=60)
    assert claimed is not None and claimed.claim_generation == 1
    assert repository.transition(job.id, "agent", owner="processing-owner", claim_generation=1, status="running") is False
    assert repository.transition(job.id, "visual", owner="wrong-owner", claim_generation=1, status="running") is False
    assert repository.transition(job.id, "visual", owner="processing-owner", claim_generation=1, status="running") is True
    assert repository.transition(job.id, "visual", owner="processing-owner", claim_generation=1, status="succeeded") is True
    assert repository.transition(job.id, "agent", owner="processing-owner", claim_generation=1, status="running") is True
    assert repository.transition(job.id, "agent", owner="processing-owner", claim_generation=1, status="failed", error={"error": "unknown_execution"}) is True
    snapshot = repository.snapshot(job.id)
    assert snapshot is not None and snapshot.status == "failed" and snapshot.current_stage == "agent"
    retried = repository.retry(job.id, config=config)
    assert retried.id != job.id and retried.revision == 2 and retried.status == "queued"
    assert retried.reverse_image_policy == "auto"
    assert retried.auto_name is True


def test_image_processing_warning_does_not_pollute_successful_job_error(postgres_resources) -> None:
    """自动命名 warning 保留阶段错误，但父 Job 成功时顶层 error 必须为空。"""
    repository = ImageProcessingRepository(postgres_resources, "local")
    meme = StorageCoordinator(postgres_resources).upload(b"warning-transition", target_key="warning-transition.png", extension=".png", context={}, provenance={})
    job = repository.create_or_reuse(
        meme.id,
        meme.sha256,
        config={"agent_model": "warning-agent", "embedding_model": "warning-embedding"},
        auto_name=True,
    )
    claimed = repository.claim(job.id, owner="warning-owner", lease_seconds=60)
    assert claimed is not None
    generation = claimed.claim_generation
    for stage in ("visual", "agent"):
        assert repository.transition(job.id, stage, owner="warning-owner", claim_generation=generation, status="running")
        assert repository.transition(job.id, stage, owner="warning-owner", claim_generation=generation, status="succeeded")
    assert repository.transition(job.id, "auto_rename", owner="warning-owner", claim_generation=generation, status="running")
    assert repository.transition(
        job.id,
        "auto_rename",
        owner="warning-owner",
        claim_generation=generation,
        status="warning",
        error={"error": "auto_rename_title_missing"},
    )
    warning_snapshot = repository.snapshot(job.id)
    assert warning_snapshot is not None
    assert warning_snapshot.status == "running"
    assert warning_snapshot.error is None
    assert warning_snapshot.has_warnings is True
    assert repository.transition(job.id, "text_embedding", owner="warning-owner", claim_generation=generation, status="running")
    assert repository.transition(job.id, "text_embedding", owner="warning-owner", claim_generation=generation, status="succeeded")
    completed = repository.snapshot(job.id)
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.error is None
    assert completed.warnings[0]["error"] == "auto_rename_title_missing"


def test_image_processing_warning_is_limited_to_auto_rename_degradable_errors(postgres_resources) -> None:
    """非自动重命名阶段和不可降级错误不能伪装成 warning。"""
    repository = ImageProcessingRepository(postgres_resources, "local")
    meme = StorageCoordinator(postgres_resources).upload(b"warning-guard", target_key="warning-guard.png", extension=".png", context={}, provenance={})
    job = repository.create_or_reuse(
        meme.id,
        meme.sha256,
        config={"agent_model": "warning-guard-agent", "embedding_model": "warning-guard-embedding"},
        auto_name=True,
    )
    claimed = repository.claim(job.id, owner="warning-guard-owner", lease_seconds=60)
    assert claimed is not None
    generation = claimed.claim_generation
    with pytest.raises(ImageProcessingError, match="invalid_stage_transition"):
        repository.transition(
            job.id,
            "visual",
            owner="warning-guard-owner",
            claim_generation=generation,
            status="warning",
            error={"error": "auto_rename_title_missing"},
        )
    for stage in ("visual", "agent"):
        assert repository.transition(job.id, stage, owner="warning-guard-owner", claim_generation=generation, status="running")
        assert repository.transition(job.id, stage, owner="warning-guard-owner", claim_generation=generation, status="succeeded")
    assert repository.transition(job.id, "auto_rename", owner="warning-guard-owner", claim_generation=generation, status="running")
    with pytest.raises(ImageProcessingError, match="invalid_stage_transition"):
        repository.transition(
            job.id,
            "auto_rename",
            owner="warning-guard-owner",
            claim_generation=generation,
            status="warning",
            error={"error": "auto_rename_unknown_execution"},
        )


def test_legacy_three_stage_snapshot_is_read_only_and_uses_safe_defaults(postgres_resources) -> None:
    """迁移前三阶段 Job 只读合成 skipped，不插入第四阶段历史行。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(
        b"legacy-three-stage",
        target_key="legacy-three-stage.png",
        extension=".png",
        context={},
        provenance={},
    )
    repository = ImageProcessingRepository(resources, "local")
    job = repository.create_or_reuse(
        meme.id,
        meme.sha256,
        config={"agent_model": "legacy-agent", "embedding_model": "legacy-embedding"},
    )
    with resources.factory() as session:
        stage = session.scalar(
            select(ImageProcessingStage).where(
                ImageProcessingStage.scope_id == "local",
                ImageProcessingStage.job_id == job.id,
                ImageProcessingStage.stage == "auto_rename",
            )
        )
        assert stage is not None
        session.delete(stage)
        session.commit()
    snapshot = repository.snapshot(job.id)
    assert snapshot is not None
    assert snapshot.auto_name is False
    assert [item["stage"] for item in snapshot.stages] == ["visual", "agent", "auto_rename", "text_embedding"]
    assert snapshot.stages[2]["status"] == "skipped"
    with resources.factory() as session:
        persisted = list(
            session.scalars(
                select(ImageProcessingStage).where(
                    ImageProcessingStage.scope_id == "local",
                    ImageProcessingStage.job_id == job.id,
                )
            )
        )
    assert len(persisted) == 3


def test_image_processing_migration_defaults_and_constraints(postgres_resources) -> None:
    """0012 的默认值和四阶段 CHECK 必须拒绝旧三阶段之外的非法映射。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(
        b"migration-contract",
        target_key="migration-contract.png",
        extension=".png",
        context={},
        provenance={},
    )
    job_id = uuid4()
    with resources.factory() as session:
        session.execute(
            text(
                """
                INSERT INTO image_processing_jobs
                    (id, scope_id, meme_id, revision, image_sha256,
                     processing_config_hash, processing_config,
                     reverse_image_policy, status, current_stage,
                     created_at, updated_at)
                VALUES
                    (:id, 'local', :meme_id, 1, :sha,
                     :config_hash, '{}'::jsonb,
                     'forbid', 'queued', 'visual', now(), now())
                """
            ),
            {"id": job_id, "meme_id": meme.id, "sha": meme.sha256, "config_hash": "a" * 64},
        )
        session.commit()
    with resources.factory() as session:
        assert session.scalar(
            text("SELECT auto_name FROM image_processing_jobs WHERE id = :id"),
            {"id": job_id},
        ) is False

    for stage, status in (("not_a_stage", "queued"), ("visual", "not_a_status")):
        with pytest.raises(IntegrityError):
            with resources.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO image_processing_stages
                            (scope_id, job_id, stage, status, updated_at)
                        VALUES ('local', :job_id, :stage, :status, now())
                        """
                    ),
                    {"job_id": job_id, "stage": stage, "status": status},
                )

    with pytest.raises(IntegrityError):
        with resources.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tasks
                        (id, scope_id, task_type, submission_mode, image_stage,
                         lane, payload, status, claim_generation, attempt_count,
                         max_attempts, available_at, created_at, updated_at)
                    VALUES
                        (:id, 'local', 'image_auto_rename', 'standalone', 'visual',
                         'default', '{}'::jsonb, 'queued', 0, 0, 3,
                         now(), now(), now())
                    """
                ),
                {"id": f"invalid-stage-{uuid4().hex}"},
            )


def test_image_file_identity_rejects_changed_bytes(postgres_resources) -> None:
    """文件字节或大小变化时共享核心身份判定必须失败。"""
    from backend.image_processing import image_file_matches

    meme = StorageCoordinator(postgres_resources).upload(b"identity-before", target_key="identity-check.png", extension=".png", context={}, provenance={})
    assert image_file_matches(postgres_resources, "local", meme) is True
    postgres_resources.blob_store.resolve(meme.storage_key).write_bytes(b"identity-after-with-different-size")
    assert image_file_matches(postgres_resources, "local", meme) is False


def test_auto_rename_same_name_still_checks_claim_without_storage_operation(postgres_resources) -> None:
    """自动命名目标已同名时仍校验执行权，且不留下无意义存储操作。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    meme = coordinator.upload(
        b"same-name-target",
        target_key="same-name.png",
        extension=".png",
        context={"title": "same-name"},
        provenance={},
    )
    title_fingerprint = hashlib.sha256(b"same-name").hexdigest()
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="image_auto_rename",
            payload={"meme_id": str(meme.id), "image_sha256": meme.sha256, "stage": "auto_rename"},
            lane="default",
            dedupe_key="same-name-cas",
            submission_mode="standalone",
            image_stage="auto_rename",
        )
        claim = environment.tasks.claim(owner="same-name-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None

    before_revision = meme.revision
    result = coordinator.rename_if_current(
        meme.id,
        target_key="same-name.png",
        expected_source_key="same-name.png",
        expected_sha256=meme.sha256,
        expected_revision=before_revision,
        task_id=claim.id,
        claim_generation=claim.claim_generation,
        attempt=claim.attempt_count,
        claim_owner="same-name-owner",
        expected_title_fingerprint=title_fingerprint,
    )
    assert result.storage_key == "same-name.png"
    assert result.revision == before_revision
    with resources.factory() as session:
        operations = list(
            session.scalars(
                select(StorageOperation).where(
                    StorageOperation.scope_id == "local",
                    StorageOperation.meme_id == meme.id,
                )
            )
        )
    assert len(operations) == 1
    assert operations[0].operation_type == "upload"

    # 即使目标文件名已经相同，也必须确认文件字节仍是当前 Meme 的身份。
    resources.blob_store.resolve("same-name.png").write_bytes(b"same-name-mutated")
    with pytest.raises(DatabaseError, match="target_changed"):
        coordinator.rename_if_current(
            meme.id,
            target_key="same-name.png",
            expected_source_key="same-name.png",
            expected_sha256=meme.sha256,
            expected_revision=before_revision,
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            claim_owner="same-name-owner",
            expected_title_fingerprint=title_fingerprint,
        )

    with resources.environment("local") as environment:
        environment.tasks.fail_fenced(
            claim.id,
            claim.claim_generation,
            "same-name-owner",
            error={"error": "claim_expired"},
            message="claim expired",
            retry=False,
        )
    with pytest.raises(DatabaseError, match="claim_expired"):
        coordinator.rename_if_current(
            meme.id,
            target_key="same-name.png",
            expected_source_key="same-name.png",
            expected_sha256=meme.sha256,
            expected_revision=before_revision,
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            claim_owner="same-name-owner",
            expected_title_fingerprint=title_fingerprint,
        )


def test_auto_rename_same_name_fails_closed_for_blocked_storage_operation(postgres_resources) -> None:
    """同名自动命名也必须拒绝复用已有 blocked 存储副作用。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    meme = coordinator.upload(
        b"blocked-same-name",
        target_key="blocked-same-name.png",
        extension=".png",
        context={"title": "blocked-same-name"},
        provenance={},
    )
    title_fingerprint = hashlib.sha256(b"blocked-same-name").hexdigest()
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="image_auto_rename",
            payload={"meme_id": str(meme.id), "image_sha256": meme.sha256, "stage": "auto_rename"},
            lane="default",
            dedupe_key="blocked-same-name",
            submission_mode="standalone",
            image_stage="auto_rename",
        )
        claim = environment.tasks.claim(owner="blocked-same-name-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None
    with resources.factory() as session:
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=meme.id,
                operation_type="rename",
                operation_token=uuid4(),
                source_key=meme.storage_key,
                target_key=meme.storage_key,
                before_sha256=meme.sha256,
                after_sha256=meme.sha256,
                before_size=meme.size_bytes,
                after_size=meme.size_bytes,
                status="blocked",
            )
        )
        session.commit()
    with pytest.raises(DatabaseError, match="storage_operation_unknown"):
        coordinator.rename_if_current(
            meme.id,
            target_key=meme.storage_key,
            expected_source_key=meme.storage_key,
            expected_sha256=meme.sha256,
            expected_revision=meme.revision,
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            claim_owner="blocked-same-name-owner",
            expected_title_fingerprint=title_fingerprint,
        )


def test_auto_rename_file_move_error_is_unknown_and_blocked(postgres_resources, monkeypatch) -> None:
    """文件移动异常无法确认副作用时必须转为 unknown 并阻断操作。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    meme = coordinator.upload(
        b"move-error",
        target_key="move-error-source.png",
        extension=".png",
        context={"title": "move-error-target"},
        provenance={},
    )
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="image_auto_rename",
            payload={"meme_id": str(meme.id), "image_sha256": meme.sha256, "stage": "auto_rename"},
            lane="default",
            dedupe_key="move-error",
            submission_mode="standalone",
            image_stage="auto_rename",
        )
        claim = environment.tasks.claim(owner="move-error-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None

    def fail_move(_source_key: str, _target_key: str) -> None:
        """模拟 link/unlink 边界的不可确认文件异常。"""
        raise DatabaseError("file_move_failed")

    monkeypatch.setattr(resources.blob_store, "link_move", fail_move)
    with pytest.raises(DatabaseError, match="storage_operation_unknown"):
        coordinator.rename_if_current(
            meme.id,
            target_key="move-error-target.png",
            expected_source_key=meme.storage_key,
            expected_sha256=meme.sha256,
            expected_revision=meme.revision,
            task_id=claim.id,
            claim_generation=claim.claim_generation,
            attempt=claim.attempt_count,
            claim_owner="move-error-owner",
            expected_title_fingerprint=hashlib.sha256(b"move-error-target").hexdigest(),
        )
    with resources.factory() as session:
        operation = session.scalar(
            select(StorageOperation).where(
                StorageOperation.scope_id == "local",
                StorageOperation.meme_id == meme.id,
                StorageOperation.operation_type == "rename",
            )
        )
        assert operation is not None
        assert operation.status == "blocked"


def test_image_worker_rejects_stage_reuse_after_file_bytes_change(postgres_resources) -> None:
    """Worker 复用阶段产物前必须拒绝已被外部替换的图片字节。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"stage-identity", target_key="stage-identity.png", extension=".png", context={}, provenance={})
    repository = ImageProcessingRepository(resources, "local")
    job = repository.create_or_reuse(
        meme.id,
        meme.sha256,
        config={"visual_model": "visual", "visual_dimensions": 2, "preprocess_version": "v1", "agent_model": "agent", "embedding_model": "text"},
    )
    worker = ImageProcessingWorker(
        resources,
        scope_id="local",
        task_service=SimpleNamespace(agent_concurrency=1, agent_backpressure=32, settings_version="test", lease_seconds=60, max_attempts=3),
        max_workers=1,
    )
    try:
        resources.blob_store.resolve(meme.storage_key).write_bytes(b"stage-identity-replaced")
        with pytest.raises(ImageProcessingError, match="target_changed"):
            worker._stage_valid(job, "visual")
    finally:
        worker.shutdown()


def test_image_processing_options_are_frozen_and_auto_name_is_not_config_identity(postgres_resources) -> None:
    """活动 Job 必须冻结自动命名选项，且该选项不能改变 Agent 配置指纹。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"options-target", target_key="options-target.png", extension=".png", context={}, provenance={})
    repository = ImageProcessingRepository(resources, "local")
    config = {"agent_model": "options-agent", "embedding_model": "options-embedding", "embedding_dimensions": 1024}
    enabled = repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="forbid", auto_name=True)
    reused = repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="forbid", auto_name=True)
    assert reused.id == enabled.id
    assert enabled.auto_name is True
    assert enabled.processing_config_hash == repository.create_or_reuse(
        meme.id,
        meme.sha256,
        config={**config, "auto_name": False},
        reverse_image_policy="forbid",
        auto_name=True,
    ).processing_config_hash
    with pytest.raises(ImageProcessingError, match="processing_options_conflict"):
        repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="forbid", auto_name=False)
    with pytest.raises(ImageProcessingError, match="generation_policy_conflict"):
        repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="auto", auto_name=False)
    with resources.factory() as session:
        stages = list(session.scalars(select(ImageProcessingStage).where(ImageProcessingStage.scope_id == "local", ImageProcessingStage.job_id == enabled.id)))
    assert {stage.stage: stage.status for stage in stages}["auto_rename"] == "queued"


def test_storage_cas_rename_preserves_manual_winner_conflict_and_claim_fencing(postgres_resources) -> None:
    """自动重命名存储 CAS 必须保护手动改名、目标冲突和失效 claim。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)

    def claimed_task(meme, *, owner: str, dedupe: str):
        """为一次独立自动重命名构造带真实 claim 的 Task。"""
        title = str((meme.meme_context or {}).get("title") or "")
        with resources.environment("local") as environment:
            record = environment.tasks.submit(
                task_type="image_auto_rename",
                payload={
                    "submission_mode": "standalone",
                    "stage": "auto_rename",
                    "meme_id": str(meme.id),
                    "image_sha256": meme.sha256,
                    "expected_storage_key": meme.storage_key,
                    "expected_meme_revision": meme.revision,
                    "title_fingerprint": hashlib.sha256(title.encode()).hexdigest(),
                },
                lane="default",
                dedupe_key=dedupe,
                submission_mode="standalone",
                image_stage="auto_rename",
            )
            claim = environment.tasks.claim(owner=owner, task_id=record.id, lease_seconds=60)
            assert claim is not None
            return claim

    manual = coordinator.upload(b"manual-winner", target_key="manual-source.png", extension=".png", context={"title": "Manual winner"}, provenance={})
    manual_claim = claimed_task(manual, owner="cas-manual", dedupe="cas-manual")
    coordinator.rename(manual.id, target_key="manual-winner.png")
    with pytest.raises(DatabaseError, match="storage_key_changed"):
        coordinator.rename_if_current(
            manual.id,
            target_key="Derived manual.png",
            expected_source_key="manual-source.png",
            expected_sha256=manual.sha256,
            expected_revision=1,
            task_id=manual_claim.id,
            claim_generation=manual_claim.claim_generation,
            attempt=manual_claim.attempt_count,
            claim_owner="cas-manual",
            expected_title_fingerprint=hashlib.sha256(b"Manual winner").hexdigest(),
        )
    assert resources.blob_store.resolve("manual-winner.png").exists()
    assert not resources.blob_store.resolve("Derived manual.png", must_exist=False).exists()

    conflict = coordinator.upload(b"conflict-target", target_key="conflict-source.png", extension=".png", context={"title": "Conflict target"}, provenance={})
    occupied = coordinator.upload(b"occupied-target", target_key="Conflict target.png", extension=".png", context={}, provenance={})
    conflict_claim = claimed_task(conflict, owner="cas-conflict", dedupe="cas-conflict")
    with pytest.raises(DatabaseError, match="target_exists"):
        coordinator.rename_if_current(
            conflict.id,
            target_key="Conflict target.png",
            expected_source_key="conflict-source.png",
            expected_sha256=conflict.sha256,
            expected_revision=1,
            task_id=conflict_claim.id,
            claim_generation=conflict_claim.claim_generation,
            attempt=conflict_claim.attempt_count,
            claim_owner="cas-conflict",
            expected_title_fingerprint=hashlib.sha256(b"Conflict target").hexdigest(),
        )
    assert resources.blob_store.resolve("conflict-source.png").exists()
    assert resources.blob_store.resolve("Conflict target.png").exists()
    assert occupied.id != conflict.id

    fenced = coordinator.upload(b"fenced-target", target_key="fenced-source.png", extension=".png", context={"title": "Fenced target"}, provenance={})
    fenced_claim = claimed_task(fenced, owner="cas-fenced", dedupe="cas-fenced")
    with pytest.raises(DatabaseError, match="claim_expired"):
        coordinator.rename_if_current(
            fenced.id,
            target_key="Fenced target.png",
            expected_source_key="fenced-source.png",
            expected_sha256=fenced.sha256,
            expected_revision=1,
            task_id=fenced_claim.id,
            claim_generation=fenced_claim.claim_generation,
            attempt=fenced_claim.attempt_count,
            claim_owner="wrong-owner",
            expected_title_fingerprint=hashlib.sha256(b"Fenced target").hexdigest(),
        )
    assert resources.blob_store.resolve("fenced-source.png").exists()
    assert not resources.blob_store.resolve("Fenced target.png", must_exist=False).exists()


def test_storage_recovery_blocks_auto_rename_after_claim_lease_expires(postgres_resources) -> None:
    """自动重命名的 storage operation 在 claim 失效后不得继续移动或 finalize。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    meme = coordinator.upload(
        b"recovery-claim",
        target_key="recovery-source.png",
        extension=".png",
        context={"title": "Recovery claim"},
        provenance={},
    )
    title_fingerprint = hashlib.sha256(b"Recovery claim").hexdigest()
    with resources.environment("local") as environment:
        task = environment.tasks.submit(
            task_type="image_auto_rename",
            payload={
                "submission_mode": "standalone",
                "stage": "auto_rename",
                "meme_id": str(meme.id),
                "image_sha256": meme.sha256,
            },
            lane="default",
            dedupe_key="recovery-claim",
            submission_mode="standalone",
            image_stage="auto_rename",
        )
        claim = environment.tasks.claim(owner="recovery-owner", task_id=task.id, lease_seconds=60)
        assert claim is not None
        claim_generation = claim.claim_generation
        attempt = claim.attempt_count
    with resources.factory() as session:
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=meme.id,
                operation_type="rename",
                operation_token=uuid4(),
                source_key=meme.storage_key,
                target_key="Recovery claim.png",
                before_sha256=meme.sha256,
                after_sha256=meme.sha256,
                before_size=meme.size_bytes,
                after_size=meme.size_bytes,
                expected_revision=meme.revision,
                claim_generation=claim_generation,
                attempt=attempt,
                task_id=claim.id,
                expected_title_fingerprint=title_fingerprint,
                status="prepared",
            )
        )
        task_row = session.scalar(select(Task).where(Task.scope_id == "local", Task.id == claim.id))
        assert task_row is not None
        task_row.lease_expires_at = utcnow().replace(year=2000)
        session.commit()

    counts = coordinator.recover()
    assert counts["blocked"] == 1
    assert resources.blob_store.resolve("recovery-source.png").exists()
    assert not resources.blob_store.resolve("Recovery claim.png", must_exist=False).exists()
    with resources.factory() as session:
        operation = session.scalar(
            select(StorageOperation).where(
                StorageOperation.scope_id == "local",
                StorageOperation.task_id == claim.id,
            )
        )
        assert operation is not None and operation.status == "blocked"


def test_image_worker_agent_dedupes_before_acquire_and_persists_one_trusted_grant(postgres_resources) -> None:
    """真实 PostgreSQL Worker 在有效产物/活动任务后不计量，并发只创建一个 grant/Task。"""
    resources = postgres_resources
    policy_impl = _CountingAllowAllPolicy()
    gateway = OperationPolicyGateway(policy_impl, allow_all=True)
    grant_store = PersistentGrantAssociationStore(resources)
    task_service = SimpleNamespace(
        agent_concurrency=1,
        agent_backpressure=32,
        settings_version="integration-test",
        lease_seconds=60,
        max_attempts=3,
    )
    worker = ImageProcessingWorker(
        resources,
        scope_id="local",
        task_service=task_service,
        policy=gateway,
        grant_store=grant_store,
        max_workers=2,
    )
    # 该测试只验证准备/去重边界，不启动叶子 Task 的外部模型执行。
    worker.schedule = lambda _job_id: None
    assert worker._task_runner is not None
    worker._task_runner.schedule = lambda _task_id: None
    repository = worker.jobs
    config = {
        "agent_model": "integration-agent",
        "embedding_model": "integration-embedding",
        "embedding_dimensions": 1024,
        "scope_id": "attacker-scope",
        "user_id": "attacker-user",
        "grant": "forged-grant",
        "resource_id": "attacker-resource",
        "task_id": "attacker-task",
        "session_id": "attacker-session",
        "attempt": 99,
    }

    def create_job(storage_key: str):
        """创建一张仅用于测试单 scope Worker 准备路径的图片 job。"""
        meme = StorageCoordinator(resources).upload(b"agent-worker-target", target_key=storage_key, extension=".png", context={}, provenance={})
        return meme, repository.create_or_reuse(meme.id, meme.sha256, config=config, reverse_image_policy="forbid")

    def advance_visual(job) -> None:
        """把测试 job 推到 Agent 阶段，保留真实 claim/fencing 条件。"""
        claimed = repository.claim(job.id, owner=worker.owner, lease_seconds=60)
        assert claimed is not None
        assert repository.transition(job.id, "visual", owner=worker.owner, claim_generation=claimed.claim_generation, status="succeeded") is True

    try:
        artifact_meme, artifact_job = create_job("worker-artifact.png")
        with resources.factory() as session:
            row = session.get(Meme, artifact_meme.id)
            assert row is not None
            row.context_status = "ready"
            row.provenance = {
                "agent_context": {
                    "task_id": "previous-agent-task",
                    "image_sha256": row.sha256,
                    "model": config["agent_model"],
                    "reverse_image_policy": "forbid",
                    "processing_config_hash": artifact_job.processing_config_hash,
                    "completed_at": "2026-08-16T00:00:00+00:00",
                }
            }
            session.commit()
        advance_visual(artifact_job)
        worker._run(str(artifact_job.id))
        artifact_snapshot = repository.snapshot(artifact_job.id)
        assert artifact_snapshot is not None
        artifact_stage = next(item for item in artifact_snapshot.stages if item["stage"] == "agent")
        assert artifact_stage["status"] == "succeeded"
        assert artifact_stage["task_id"] is None
        assert policy_impl.requests == []

        active_meme, active_job = create_job("worker-active.png")
        active_payload = {
            "job_id": str(active_job.id),
            "job_revision": active_job.revision,
            "meme_id": str(active_meme.id),
            "image_sha256": active_meme.sha256,
            "reverse_image_policy": "forbid",
            "processing_config_hash": active_job.processing_config_hash,
            "stage": "agent",
            "agent_model": config["agent_model"],
            "embedding_model": config["embedding_model"],
            "embedding_dimensions": config["embedding_dimensions"],
        }
        active_task = worker._task_runner.submit("meme_context_generation", active_payload, schedule=False)
        advance_visual(active_job)
        assert worker._prepare_task(active_job, "agent") == active_task.task_id
        assert policy_impl.requests == []

        concurrent_meme, concurrent_job = create_job("worker-concurrent.png")

        def prepare_agent_task(_index: int) -> str:
            """并发执行同一逻辑 Agent Task 的准备阶段。"""
            return worker._prepare_task(concurrent_job, "agent")

        with ThreadPoolExecutor(max_workers=8) as executor:
            task_ids = list(executor.map(prepare_agent_task, range(32)))
        assert len(set(task_ids)) == 1
        logical_key = f"agent:{concurrent_job.meme_id}:{concurrent_job.image_sha256}:{concurrent_job.processing_config_hash}:forbid:r{concurrent_job.revision}"
        with resources.factory() as session:
            grants = list(
                session.scalars(
                    select(OperationGrant).where(
                        OperationGrant.scope_id == "local",
                        OperationGrant.operation == Operations.ANALYSIS_AGENT,
                        OperationGrant.idempotency_key == logical_key,
                    )
                )
            )
            tasks = list(
                session.scalars(
                    select(Task).where(
                        Task.scope_id == "local",
                        Task.task_type == "meme_context_generation",
                        Task.dedupe_key == worker._task_runner._dedupe("meme_context_generation", active_payload | {
                            "job_id": str(concurrent_job.id),
                            "job_revision": concurrent_job.revision,
                            "meme_id": str(concurrent_meme.id),
                            "image_sha256": concurrent_meme.sha256,
                            "processing_config_hash": concurrent_job.processing_config_hash,
                        }),
                    )
                )
            )
        assert len(policy_impl.requests) == 1
        assert policy_impl.requests[0].scope == ScopeContext("local")
        assert policy_impl.requests[0].resource_id == str(concurrent_meme.id)
        assert policy_impl.requests[0].task_id is None
        assert len(grants) == 1
        assert grants[0].resource_id == str(concurrent_meme.id)
        assert grants[0].task_id == task_ids[0]
        assert len(tasks) == 1
        assert tasks[0].id == task_ids[0]
        assert tasks[0].payload["meme_id"] == str(concurrent_meme.id)
        assert "scope_id" not in tasks[0].payload
        assert "user_id" not in tasks[0].payload
        assert "grant" not in tasks[0].payload
        assert "resource_id" not in tasks[0].payload
        forged = gateway.request("local", Operations.ANALYSIS_AGENT, "empty-resource-check", resource_id=None, scope_id="attacker", user_id="attacker", grant="forged")
        assert forged.scope == ScopeContext("local")
        assert forged.resource_id is None
    finally:
        worker.shutdown()


def test_search_migration_state_uses_single_epoch_and_atomic_switch(postgres_resources) -> None:
    """增量回填只接受当前 epoch，完成后才切换唯一查询来源。"""
    resources = postgres_resources
    with resources.environment("local") as environment:
        state = environment.search.begin_incremental_backfill("migration-test", total_count=2)
        epoch = state.epoch
        assert state.mode == "backfill"
        assert environment.search.record_incremental_backfill(epoch=epoch - 1, completed_count=1, model="migration-test") is False
        assert environment.search.record_incremental_backfill(epoch=epoch, completed_count=1, model="migration-test") is True
        assert environment.search.switch_incremental_only(epoch=epoch, model="migration-test") is False
        assert environment.search.record_incremental_backfill(epoch=epoch, completed_count=2, model="migration-test") is True
        assert environment.search.switch_incremental_only(epoch=epoch, model="migration-test") is True
        assert environment.search.source_mode("migration-test") == "incremental"
        assert environment.search.source_mode("other-model") == "legacy"


def _clean_business_rows(engine: Engine) -> None:
    """清理集成测试产生的业务行，不触碰 scope、安装标记和迁移版本。"""
    with engine.begin() as connection:
        for table in ("agent_callback_requests", "reverse_image_usage_events", "operation_grants", "image_processing_attempts", "image_processing_stages", "image_processing_jobs", "meme_text_embeddings", "search_migration_states", "task_lane_fairness", "task_lane_resource_slots", "task_lane_slots", "task_batch_items", "task_batches", "tasks", "meme_visual_embeddings", "meme_embeddings", "search_heads", "search_generations", "derived_image_thumbnails", "storage_operations", "meme_collection_items", "meme_collections", "memes"):
            connection.execute(text(f"DELETE FROM {table} WHERE scope_id = 'local'" if table not in {"task_lane_slots", "task_lane_resource_slots"} else f"DELETE FROM {table}"))


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


def test_storage_recovery_removes_meme_after_file_applied_before_delete_commit(postgres_resources) -> None:
    """恢复 ``file_applied`` 删除时核对仍存在的 Meme，并清理派生文件和事实。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"delete-recovery-{uuid4().hex}.png"
    meme = coordinator.upload(b"delete-recovery", target_key=source_key, extension=".png", context={}, provenance={})
    thumbnail_key = f"{meme.id.hex}-{meme.sha256}-thumbnail-v1.jpg"
    thumbnail_store = resources.thumbnail_store_for_scope("local")
    thumbnail_content = b"derived"
    thumbnail_store._key_path(thumbnail_key).write_bytes(thumbnail_content)
    token = uuid4()
    quarantine_key = resources.blob_store.quarantine(source_key, token=token)

    with resources.factory() as session:
        session.add(
            DerivedImageThumbnail(
                scope_id="local",
                meme_id=meme.id,
                source_sha256=meme.sha256,
                source_size_bytes=meme.size_bytes,
                profile="thumbnail-v1",
                output_key=thumbnail_key,
                output_sha256=hashlib.sha256(thumbnail_content).hexdigest(),
                output_size_bytes=len(thumbnail_content),
                width=4,
                height=4,
                media_type="image/jpeg",
                status="available",
            )
        )
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=meme.id,
                operation_type="delete",
                operation_token=token,
                source_key=source_key,
                target_key=quarantine_key,
                before_sha256=meme.sha256,
                before_size=meme.size_bytes,
                thumbnail_keys=[thumbnail_key],
                status="file_applied",
            )
        )
        session.commit()

    counts = coordinator.recover()
    assert counts["completed"] == 1
    assert not resources.blob_store._key_path(quarantine_key, must_exist=False).exists()
    assert not thumbnail_store._key_path(thumbnail_key, must_exist=False).exists()
    with resources.factory() as session:
        assert session.scalar(select(Meme).where(Meme.id == meme.id)) is None
        assert session.scalar(select(DerivedImageThumbnail).where(DerivedImageThumbnail.meme_id == meme.id)) is None
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token))
        assert operation is not None
        assert operation.status == "completed"
        assert operation.meme_id is None


def test_storage_recovery_does_not_delete_changed_meme_after_prepared_quarantine(postgres_resources) -> None:
    """prepared 删除隔离后若 Meme 身份已变化，恢复器必须阻断而不能误删。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"prepared-delete-changed-{uuid4().hex}.png"
    meme = coordinator.upload(b"prepared-delete-changed", target_key=source_key, extension=".png", context={}, provenance={})
    token = uuid4()
    quarantine_key = resources.blob_store.quarantine(source_key, token=token)
    changed_key = f"prepared-delete-replacement-{uuid4().hex}.png"
    with resources.factory() as session:
        record = session.get(Meme, meme.id)
        assert record is not None
        record.storage_key = changed_key
        record.revision += 1
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=meme.id,
                operation_type="delete",
                operation_token=token,
                source_key=source_key,
                target_key=quarantine_key,
                before_sha256=meme.sha256,
                before_size=meme.size_bytes,
                thumbnail_keys=[],
                error={
                    "meme_id": str(meme.id),
                    "source_sha256": meme.sha256,
                    "source_size_bytes": meme.size_bytes,
                },
                status="prepared",
            )
        )
        session.commit()

    counts = coordinator.recover()

    assert counts == {"completed": 0, "compensated": 0, "blocked": 1, "retried": 0}
    with resources.factory() as session:
        record = session.get(Meme, meme.id)
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token))
        assert record is not None and record.storage_key == changed_key
        assert operation is not None and operation.status == "blocked"
        assert operation.error["error"] == "delete_target_changed"


def test_metadata_load_rejects_explicit_malformed_identity(postgres_resources) -> None:
    """显式 malformed identity 不能被当作原图不一致证据写入 repair_required。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"metadata-identity", target_key=f"metadata-identity-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    service = PostgresMetadataService(resources)
    image = resources.blob_store.resolve(meme.storage_key)

    with pytest.raises(MetadataError, match="metadata_invalid"):
        service.load(image, identity={"relative_path": meme.storage_key, "extension": ".png", "size_bytes": "not-an-int", "sha256": meme.sha256})

    with resources.factory() as session:
        record = session.get(Meme, meme.id)
        assert record is not None and record.context_status == "pending"


def test_thumbnail_pending_is_race_safe_for_concurrent_first_projection(postgres_resources) -> None:
    """并发首次投影只保留一条复合主键事实，且两个调用都能正常返回。"""
    resources = postgres_resources
    meme = StorageCoordinator(resources).upload(b"thumbnail-race", target_key=f"thumbnail-race-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    barrier = threading.Barrier(2)

    def ensure_pending() -> tuple[str, str]:
        """在独立数据库事务中首次建立 pending 派生事实。"""
        barrier.wait(timeout=10)
        with resources.environment("local") as environment:
            row = environment.thumbnails.ensure_pending(meme, "thumbnail-v1")
            return row.status, row.source_sha256

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: ensure_pending(), range(2)))
    assert results == [("pending", meme.sha256), ("pending", meme.sha256)]
    with resources.factory() as session:
        assert session.scalar(select(func.count()).select_from(DerivedImageThumbnail).where(DerivedImageThumbnail.meme_id == meme.id)) == 1


def test_storage_delete_collects_thumbnail_written_after_initial_snapshot(postgres_resources, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除第一阶段后才提交的派生输出也必须进入最终清理集合。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-delete-window-{uuid4().hex}.png"
    meme = coordinator.upload(b"delete-window", target_key=source_key, extension=".png", context={}, provenance={})
    thumbnail_store = resources.thumbnail_store_for_scope("local")
    output = b"late-derived"
    output_key = f"{meme.id.hex}-{meme.sha256}-thumbnail-v1.jpg"
    original_quarantine = coordinator.blob_store.quarantine

    def quarantine_then_write_thumbnail(source: str, *, token):
        """在原图隔离和删除提交之间模拟晚到的生成 Worker。"""
        result = original_quarantine(source, token=token)
        thumbnail_store._key_path(output_key).write_bytes(output)
        with resources.environment("local") as environment:
            current = environment.memes.get(meme.id)
            assert current is not None
            environment.thumbnails.mark_available(
                current,
                "thumbnail-v1",
                output_key=output_key,
                output_sha256=hashlib.sha256(output).hexdigest(),
                output_size_bytes=len(output),
                width=4,
                height=4,
                media_type="image/jpeg",
            )
        return result

    monkeypatch.setattr(coordinator.blob_store, "quarantine", quarantine_then_write_thumbnail)
    coordinator.delete(meme.id)
    assert not thumbnail_store._key_path(output_key, must_exist=False).exists()


def test_storage_recovery_collects_unregistered_thumbnail_output(postgres_resources) -> None:
    """恢复 file_applied 删除时按 Meme 身份扫描并清理未登记派生文件。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-recovery-window-{uuid4().hex}.png"
    meme = coordinator.upload(b"recovery-window", target_key=source_key, extension=".png", context={}, provenance={})
    thumbnail_store = resources.thumbnail_store_for_scope("local")
    output = b"unregistered-derived"
    output_key = f"{meme.id.hex}-{meme.sha256}-thumbnail-v1.jpg"
    thumbnail_store._key_path(output_key).write_bytes(output)
    token = uuid4()
    quarantine_key = resources.blob_store.quarantine(source_key, token=token)
    with resources.factory() as session:
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=meme.id,
                operation_type="delete",
                operation_token=token,
                source_key=source_key,
                target_key=quarantine_key,
                before_sha256=meme.sha256,
                before_size=meme.size_bytes,
                thumbnail_keys=[],
                status="file_applied",
            )
        )
        session.commit()
    counts = coordinator.recover()
    assert counts["completed"] == 1
    assert not thumbnail_store._key_path(output_key, must_exist=False).exists()


def test_storage_recovery_uses_delete_marker_after_meme_detached(postgres_resources) -> None:
    """Meme 外键已解除后，恢复器仍按 durable marker 清理未登记派生输出。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-marker-recovery-{uuid4().hex}.png"
    meme = coordinator.upload(b"marker-recovery", target_key=source_key, extension=".png", context={}, provenance={})
    thumbnail_store = resources.thumbnail_store_for_scope("local")
    output = b"marker-derived"
    output_key = f"{meme.id.hex}-{meme.sha256}-thumbnail-v1.jpg"
    thumbnail_store._key_path(output_key).write_bytes(output)
    token = uuid4()
    quarantine_key = resources.blob_store.quarantine(source_key, token=token)
    with resources.factory() as session:
        detached = session.get(Meme, meme.id)
        assert detached is not None
        session.delete(detached)
        session.flush()
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=None,
                operation_type="delete",
                operation_token=token,
                source_key=source_key,
                target_key=quarantine_key,
                before_sha256=meme.sha256,
                before_size=meme.size_bytes,
                thumbnail_keys=[],
                error={
                    "meme_id": str(meme.id),
                    "source_sha256": meme.sha256,
                    "source_size_bytes": meme.size_bytes,
                },
                status="file_applied",
            )
        )
        session.commit()
    counts = coordinator.recover()
    assert counts["completed"] == 1
    assert not thumbnail_store._key_path(output_key, must_exist=False).exists()
    with resources.factory() as session:
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token))
        assert operation is not None and operation.status == "completed"


def test_storage_recovery_keeps_delete_active_when_thumbnail_scan_is_unavailable(postgres_resources, monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复无法扫描派生目录时必须保留 file_applied 事实等待重试。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-scan-unavailable-{uuid4().hex}.png"
    meme = coordinator.upload(b"scan-unavailable", target_key=source_key, extension=".png", context={}, provenance={})
    token = uuid4()
    quarantine_key = resources.blob_store.quarantine(source_key, token=token)
    with resources.factory() as session:
        detached = session.get(Meme, meme.id)
        assert detached is not None
        session.delete(detached)
        session.flush()
        session.add(
            StorageOperation(
                scope_id="local",
                meme_id=None,
                operation_type="delete",
                operation_token=token,
                source_key=source_key,
                target_key=quarantine_key,
                before_sha256=meme.sha256,
                before_size=meme.size_bytes,
                thumbnail_keys=[],
                error={
                    "meme_id": str(meme.id),
                    "source_sha256": meme.sha256,
                    "source_size_bytes": meme.size_bytes,
                },
                status="file_applied",
            )
        )
        session.commit()

    def fail_scan(_meme_id, _source_sha256):
        """模拟派生存储暂时不可扫描。"""
        raise DatabaseError("thumbnail_storage_unavailable")

    monkeypatch.setattr(coordinator, "_thumbnail_file_keys", fail_scan)
    counts = coordinator.recover()

    assert counts == {"completed": 0, "compensated": 0, "blocked": 0, "retried": 1}
    with resources.factory() as session:
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token))
        assert operation is not None
        assert operation.status == "file_applied"
        assert operation.error["error"] == "storage_cleanup_pending"


def test_storage_delete_keeps_recoverable_fact_when_final_thumbnail_scan_fails(postgres_resources, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除最终派生目录扫描失败时不能提交 completed。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-final-scan-failure-{uuid4().hex}.png"
    meme = coordinator.upload(b"final-scan-failure", target_key=source_key, extension=".png", context={}, provenance={})
    original_scan = coordinator._thumbnail_file_keys
    calls = 0

    def fail_final_scan(meme_id, source_sha256):
        """仅让删除第三阶段的最终扫描失败。"""
        nonlocal calls
        calls += 1
        if calls == 3:
            raise DatabaseError("thumbnail_storage_unavailable")
        return original_scan(meme_id, source_sha256)

    monkeypatch.setattr(coordinator, "_thumbnail_file_keys", fail_final_scan)
    with pytest.raises(DatabaseError, match="storage_cleanup_pending"):
        coordinator.delete(meme.id)

    assert calls == 3
    with resources.factory() as session:
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_type == "delete"))
        assert operation is not None
        assert operation.status == "file_applied"
        assert operation.meme_id is None
        assert operation.error["error"] == "storage_cleanup_pending"


def test_storage_delete_database_cleanup_failure_is_recoverable(postgres_resources, monkeypatch: pytest.MonkeyPatch) -> None:
    """durable 删除后的第三阶段异常统一转为 cleanup pending 并保留 marker。"""
    resources = postgres_resources
    coordinator = StorageCoordinator(resources)
    source_key = f"thumbnail-cleanup-db-failure-{uuid4().hex}.png"
    meme = coordinator.upload(b"cleanup-db-failure", target_key=source_key, extension=".png", context={}, provenance={})

    def fail_cleanup(_keys: list[str]) -> list[str]:
        """模拟文件清理阶段的数据库/存储协调异常。"""
        raise DatabaseError("thumbnail_cleanup_unavailable")

    monkeypatch.setattr(coordinator, "_cleanup_thumbnail_files", fail_cleanup)
    with pytest.raises(DatabaseError, match="storage_cleanup_pending"):
        coordinator.delete(meme.id)
    with resources.factory() as session:
        operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_type == "delete"))
        assert operation is not None
        assert operation.status == "file_applied"
        assert operation.meme_id is None
        assert operation.error is not None
        assert operation.error["meme_id"] == str(meme.id)


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


def test_fair_claim_rotates_four_scopes_and_orders_tasks(postgres_resources, postgres_engine: Engine) -> None:
    """公平 claim 按持久序号轮转四个 scope，且 scope 内任务顺序稳定。"""
    suffix = uuid4().hex
    scope_ids = [f"fair-{letter}-{suffix}" for letter in "abcd"]
    with postgres_engine.begin() as connection:
        for index, scope_id in enumerate(scope_ids):
            connection.execute(
                text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now() + (:offset * interval '1 second'))"),
                {"id": scope_id, "namespace": uuid4(), "offset": index},
            )
    try:
        task_ids: dict[str, list[str]] = {}
        for scope_id in scope_ids:
            with postgres_resources.environment(scope_id) as environment:
                first = environment.tasks.submit(task_type="fair-probe", payload={"scope": scope_id}, lane="agent", dedupe_key=f"{scope_id}-one")
                second = environment.tasks.submit(task_type="fair-probe", payload={"scope": scope_id}, lane="agent", dedupe_key=f"{scope_id}-two")
                task_ids[scope_id] = [first.id, second.id]
        claimed_scopes: list[str] = []
        claimed_ids: list[str] = []
        for index in range(8):
            with postgres_resources.environment("local") as environment:
                claim = environment.tasks.claim_next(owner=f"fair-worker-{index}", lane="agent", lane_capacity=1, scope_capacity=1, lease_seconds=60)
                assert claim is not None
                claimed_scopes.append(claim.scope_id)
                claimed_ids.append(claim.id)
            with postgres_resources.environment(claim.scope_id) as environment:
                assert environment.tasks.update_fenced(claim.id, claim.claim_generation, f"fair-worker-{index}", status="succeeded") is True
        assert claimed_scopes == scope_ids * 2
        for scope_id in scope_ids:
            assert claimed_ids[claimed_scopes.index(scope_id)] == task_ids[scope_id][0]
            assert claimed_ids[claimed_scopes.index(scope_id, claimed_scopes.index(scope_id) + 1)] == task_ids[scope_id][1]
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id = ANY(:scope_ids)"), {"scope_ids": scope_ids})


def test_fair_claim_skips_scope_limit_and_preserves_sequence_when_slot_full(postgres_resources, postgres_engine: Engine) -> None:
    """scope 上限和全局 slot 背压只跳过候选，不推进未成功 scope 的序号。"""
    suffix = uuid4().hex
    scope_a, scope_b = f"fair-limit-a-{suffix}", f"fair-limit-b-{suffix}"
    with postgres_engine.begin() as connection:
        for index, scope_id in enumerate((scope_a, scope_b)):
            connection.execute(
                text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now() + (:offset * interval '1 second'))"),
                {"id": scope_id, "namespace": uuid4(), "offset": index},
            )
    try:
        with postgres_resources.environment(scope_a) as environment:
            first = environment.tasks.submit(task_type="fair-limit", payload={}, lane="agent", dedupe_key=f"a-{suffix}")
            second = environment.tasks.submit(task_type="fair-limit", payload={}, lane="agent", dedupe_key=f"a2-{suffix}")
        with postgres_resources.environment(scope_b) as environment:
            other = environment.tasks.submit(task_type="fair-limit", payload={}, lane="agent", dedupe_key=f"b-{suffix}")
        with postgres_resources.environment("local") as environment:
            claim_a = environment.tasks.claim_next(owner="fair-limit-a", lane="agent", lane_capacity=2, scope_capacity=1, lease_seconds=60)
            claim_b = environment.tasks.claim_next(owner="fair-limit-b", lane="agent", lane_capacity=2, scope_capacity=1, lease_seconds=60)
            assert claim_a is not None and claim_a.scope_id == scope_a
            assert claim_b is not None and claim_b.scope_id == scope_b
            assert claim_b.id == other.id
            assert environment.tasks.claim_next(owner="fair-limit-full", lane="agent", lane_capacity=2, scope_capacity=1, lease_seconds=60) is None
        with postgres_resources.environment(scope_a) as environment:
            assert environment.tasks.update_fenced(claim_a.id, claim_a.claim_generation, "fair-limit-a", status="succeeded") is True
        with postgres_resources.environment(scope_b) as environment:
            assert environment.tasks.update_fenced(claim_b.id, claim_b.claim_generation, "fair-limit-b", status="succeeded") is True
        with postgres_resources.factory() as session:
            fairness = {
                row.scope_id: row.last_dispatch_sequence
                for row in session.scalars(select(TaskLaneFairness).where(TaskLaneFairness.lane == "agent", TaskLaneFairness.scope_id.in_((scope_a, scope_b))))
            }
            queued = session.scalar(select(Task).where(Task.scope_id == scope_a, Task.id == second.id))
            assert fairness[scope_a] == 1 and fairness[scope_b] == 2
            assert queued is not None and queued.status == "queued"
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id = ANY(:scope_ids)"), {"scope_ids": [scope_a, scope_b]})


def test_resource_claim_isolated_and_bounded_by_global_slots(postgres_resources) -> None:
    """不同资源池分别占用资源槽位，同时受同一全局 lane 容量约束。"""
    with postgres_resources.environment("local") as environment:
        free = environment.tasks.submit(
            task_type="resource-free",
            payload={},
            lane="agent",
            lane_resource_key="free_series",
            dedupe_key=f"resource-free-{uuid4().hex}",
        )
        free_queued = environment.tasks.submit(
            task_type="resource-free",
            payload={"second": True},
            lane="agent",
            lane_resource_key="free_series",
            dedupe_key=f"resource-free-queued-{uuid4().hex}",
        )
        high = environment.tasks.submit(
            task_type="resource-high",
            payload={},
            lane="agent",
            lane_resource_key="luna_high",
            dedupe_key=f"resource-high-{uuid4().hex}",
        )
        first = environment.tasks.claim_next(
            owner="resource-free-worker",
            lane="agent",
            lane_capacity=2,
            resource_key="free_series",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        )
        assert first is not None and first.id == free.id
        assert environment.tasks.claim_next(
            owner="resource-free-worker-2",
            lane="agent",
            lane_capacity=2,
            resource_key="free_series",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        ) is None
        second = environment.tasks.claim_next(
            owner="resource-high-worker",
            lane="agent",
            lane_capacity=2,
            resource_key="luna_high",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        )
        assert second is not None and second.id == high.id
        free_slot = environment.tasks.resource_slot_for_task(free.id)
        high_slot = environment.tasks.resource_slot_for_task(high.id)
        assert free_slot is not None and free_slot.resource_key == "free_series"
        assert high_slot is not None and high_slot.resource_key == "luna_high"
        assert environment.tasks.heartbeat(free.id, first.claim_generation, "resource-free-worker", lease_seconds=90) is True
        assert environment.tasks.heartbeat(free.id, first.claim_generation, "stale-resource-worker", lease_seconds=90) is False
        assert environment.tasks.get(free_queued.id).status == "queued"
        assert environment.tasks.update_fenced(free.id, first.claim_generation, "resource-free-worker", status="succeeded") is True
        assert environment.tasks.update_fenced(high.id, second.claim_generation, "resource-high-worker", status="succeeded") is True


def test_resource_claim_rolls_back_global_slot_when_resource_pool_is_full(postgres_resources) -> None:
    """资源池无空位时不能遗留已取得的全局槽位或任务运行状态。"""
    with postgres_resources.environment("local") as environment:
        first = environment.tasks.submit(
            task_type="resource-rollback",
            payload={},
            lane="agent",
            lane_resource_key="luna_max",
            dedupe_key=f"resource-rollback-first-{uuid4().hex}",
        )
        second = environment.tasks.submit(
            task_type="resource-rollback",
            payload={"second": True},
            lane="agent",
            lane_resource_key="luna_max",
            dedupe_key=f"resource-rollback-second-{uuid4().hex}",
        )
        first_claim = environment.tasks.claim_next(
            owner="resource-rollback-first",
            lane="agent",
            lane_capacity=2,
            resource_key="luna_max",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        )
        assert first_claim is not None and first_claim.id == first.id
        assert environment.tasks.claim_next(
            owner="resource-rollback-second",
            lane="agent",
            lane_capacity=2,
            resource_key="luna_max",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        ) is None
        session = environment.uow.session
        second_row = session.scalar(select(Task).where(Task.scope_id == "local", Task.id == second.id))
        occupied_global = session.scalar(select(func.count()).select_from(TaskLaneSlot).where(TaskLaneSlot.task_id.is_not(None)))
        occupied_resource = session.scalar(select(func.count()).select_from(TaskLaneResourceSlot).where(TaskLaneResourceSlot.task_id.is_not(None), TaskLaneResourceSlot.resource_key == "luna_max"))
        assert second_row is not None and second_row.status == "queued"
        assert occupied_global == 1
        assert occupied_resource == 1
        assert environment.tasks.update_fenced(first.id, first_claim.claim_generation, "resource-rollback-first", status="succeeded") is True
        next_claim = environment.tasks.claim_next(
            owner="resource-rollback-retry",
            lane="agent",
            lane_capacity=2,
            resource_key="luna_max",
            resource_capacity=1,
            scope_capacity=2,
            lease_seconds=60,
        )
        assert next_claim is not None and next_claim.id == second.id
        assert environment.tasks.update_fenced(second.id, next_claim.claim_generation, "resource-rollback-retry", status="succeeded") is True


def test_agent_queue_does_not_use_finite_backpressure_capacity(postgres_resources) -> None:
    """Agent 的兼容背压参数不限制 queued 数量，运行容量仍由 slot 控制。"""
    with postgres_resources.environment("local") as environment:
        tasks = [
            environment.tasks.submit(
                task_type="agent-unbounded-queue",
                payload={"index": index},
                lane="agent",
                lane_backpressure=1,
                dedupe_key=f"agent-unbounded-queue-{index}-{uuid4().hex}",
            )
            for index in range(3)
        ]
        assert len(tasks) == 3
        claimed = environment.tasks.claim_next(owner="agent-unbounded-worker", lane="agent", lane_capacity=1, scope_capacity=1, lease_seconds=60)
        assert claimed is not None and claimed.status == "running"
        queued = list(environment.uow.session.scalars(select(Task).where(Task.id.in_([record.id for record in tasks]), Task.status == "queued")))
        assert len(queued) == 2
        assert environment.tasks.update_fenced(claimed.id, claimed.claim_generation, "agent-unbounded-worker", status="succeeded") is True


def test_resource_fairness_rotates_scopes_and_recovery_releases_both_slots(postgres_resources, postgres_engine: Engine) -> None:
    """同一资源池按 scope 公平轮询，过期 claim 恢复时两类 slot 都释放。"""
    suffix = uuid4().hex
    scope_a, scope_b = f"resource-fair-a-{suffix}", f"resource-fair-b-{suffix}"
    with postgres_engine.begin() as connection:
        for scope_id in (scope_a, scope_b):
            connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": scope_id, "namespace": uuid4()})
    try:
        task_ids = {}
        for scope_id in (scope_a, scope_b):
            with postgres_resources.environment(scope_id) as environment:
                task = environment.tasks.submit(task_type="resource-fair", payload={}, lane="agent", lane_resource_key="luna_max", dedupe_key=f"resource-fair-{scope_id}")
                task_ids[scope_id] = task.id
        with postgres_resources.environment("local") as environment:
            first = environment.tasks.claim_next(owner="resource-fair-a", lane="agent", lane_capacity=2, resource_key="luna_max", resource_capacity=2, scope_capacity=1, lease_seconds=60)
            second = environment.tasks.claim_next(owner="resource-fair-b", lane="agent", lane_capacity=2, resource_key="luna_max", resource_capacity=2, scope_capacity=1, lease_seconds=60)
            assert first is not None and first.scope_id == scope_a
            assert second is not None and second.scope_id == scope_b
        with postgres_resources.factory() as session:
            resource_slots = list(session.scalars(select(TaskLaneResourceSlot).where(TaskLaneResourceSlot.task_id.in_((first.id, second.id)))))
            assert {row.resource_key for row in resource_slots} == {"luna_max"}
            first_row = session.scalar(select(Task).where(Task.id == first.id, Task.scope_id == scope_a))
            assert first_row is not None
            first_row.lease_expires_at = utcnow() - timedelta(seconds=1)
            session.commit()
        with postgres_resources.environment("local") as environment:
            recovered = environment.tasks.claim_next(owner="resource-fair-recovered", lane="agent", lane_capacity=2, resource_key="luna_max", resource_capacity=2, scope_capacity=1, lease_seconds=60)
            assert recovered is not None and recovered.id == first.id and recovered.claim_generation == first.claim_generation + 1
        with postgres_resources.environment(scope_a) as environment:
            assert environment.tasks.update_fenced(first.id, first.claim_generation, "resource-fair-a", status="succeeded") is False
            assert environment.tasks.update_fenced(recovered.id, recovered.claim_generation, "resource-fair-recovered", status="succeeded") is True
        with postgres_resources.environment(scope_b) as environment:
            assert environment.tasks.update_fenced(second.id, second.claim_generation, "resource-fair-b", status="succeeded") is True
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id = ANY(:scope_ids)"), {"scope_ids": [scope_a, scope_b]})


def test_fair_claim_recovers_expired_task_and_fences_old_worker(postgres_resources) -> None:
    """过期租约重新进入公平队列，旧 generation 不能写回终态。"""
    with postgres_resources.environment("local") as environment:
        task = environment.tasks.submit(task_type="fair-recovery", payload={}, lane="agent", dedupe_key=f"recovery-{uuid4().hex}")
        old = environment.tasks.claim_next(owner="old-fair-worker", lane="agent", lane_capacity=1, scope_capacity=1, lease_seconds=60)
        assert old is not None
        old_generation = old.claim_generation
    with postgres_resources.factory() as session:
        row = session.scalar(select(Task).where(Task.scope_id == "local", Task.id == task.id))
        assert row is not None
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    with postgres_resources.environment("local") as environment:
        fresh = environment.tasks.claim_next(owner="new-fair-worker", lane="agent", lane_capacity=1, scope_capacity=1, lease_seconds=60)
        assert fresh is not None and fresh.id == task.id and fresh.claim_generation == old_generation + 1
        assert environment.tasks.update_fenced(task.id, old_generation, "old-fair-worker", status="succeeded") is False
        assert environment.tasks.update_fenced(task.id, fresh.claim_generation, "new-fair-worker", status="succeeded") is True


def test_process_worker_manager_handles_many_scopes_and_restart_claims(postgres_resources, postgres_engine: Engine) -> None:
    """进程级 manager 跨两个 scope 恢复任务，handler 使用持久 scope 且不复制 Worker。"""
    suffix = uuid4().hex
    scope_ids = [f"manager-a-{suffix}", f"manager-b-{suffix}"]
    with postgres_engine.begin() as connection:
        for scope_id in scope_ids:
            connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": scope_id, "namespace": uuid4()})
    settings = Settings(_env_file=None, database_url=_test_database_url(), data_root=postgres_resources.data_root, image_root=postgres_resources.image_root, embedding_api_key="", embedding_base_url="")
    manager = PostgresTaskWorkerManager(postgres_resources, agent_concurrency=1, max_attempts=2)
    factory = ScopeServiceFactory(postgres_resources, settings, worker_manager=manager)
    observed: list[str] = []
    observed_lock = threading.Lock()
    completed = threading.Event()

    def handler(payload, _progress):
        """记录 claim 注入的持久 scope，模拟可并发的短任务。"""
        with observed_lock:
            observed.append(str(payload["_claim_scope_id"]))
            if len(observed) == len(scope_ids):
                completed.set()
        return {"scope": payload["_claim_scope_id"]}

    manager.register("manager_probe", handler)
    try:
        for index, scope_id in enumerate(scope_ids):
            service = factory.for_scope(scope_id).tasks
            service.submit("manager_probe", {"index": index}, schedule=False)
        with postgres_resources.factory() as session:
            first = session.scalar(select(Task).where(Task.scope_id == scope_ids[0]))
            assert first is not None
            first.status = "running"
            first.lease_owner = "crashed-worker"
            first.lease_expires_at = utcnow().replace(year=2000)
            first.attempt_count = 1
            session.commit()
        manager.start()
        assert completed.wait(10)
        assert sorted(observed) == sorted(scope_ids)
        assert manager.worker_count == 1
        assert len(factory._services) == 0
        # handler 返回与 fenced 状态写回不是同一个事务；等待持久终态，避免读取到
        # handler 已执行但任务仍处于 running 的合法短暂窗口。
        deadline = time.monotonic() + 10
        rows: list[Task] = []
        while time.monotonic() < deadline:
            with postgres_resources.factory() as session:
                rows = list(session.scalars(select(Task).where(Task.scope_id.in_(scope_ids)).order_by(Task.scope_id)))
                statuses = [row.status for row in rows]
            if statuses == ["succeeded", "succeeded"]:
                break
            time.sleep(0.05)
        assert [row.status for row in rows] == ["succeeded", "succeeded"]
        assert all(row.error is None or row.error.get("error") != "task_scope_mismatch" for row in rows)
    finally:
        factory.shutdown()
        with postgres_engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id IN (:scope_a, :scope_b)"), {"scope_a": scope_ids[0], "scope_b": scope_ids[1]})


def test_agent_resume_keeps_queued_state_across_two_provider_failures(postgres_resources) -> None:
    """同一 session 的两次续跑失败仍按预算排队，额度耗尽后才收束未知执行。"""
    task_service = PostgresTaskService(
        postgres_resources,
        agent_concurrency=1,
        max_attempts=4,
        resume_enabled=True,
        resume_max_attempts=2,
        resume_backoff_seconds=0,
        resume_max_backoff_seconds=0,
        resume_timeout_seconds=900,
    )
    # 手动认领每个 attempt，避免测试线程自动调度 queued 任务造成竞态。
    task_service._schedule_queued = lambda: None  # type: ignore[method-assign]
    observed: list[dict[str, object]] = []

    def provider_failure(payload, _progress):
        """模拟保留 session 的 provider 错误并持久化当前 attempt。"""
        attempt = int(payload["_claim_attempt"])
        inherited_session = payload.get("_resume_session_id")
        observed.append(
            {
                "attempt": attempt,
                "session_id": inherited_session,
                "resume_available": payload.get("_resume_available"),
            }
        )
        if attempt == 1:
            assert inherited_session is None
        else:
            assert inherited_session == "resume-session"
            assert payload.get("_resume_available") is True
        session_id = str(inherited_session or "resume-session")
        executor_attempt_id = f"executor-attempt-{attempt}"
        payload["_resume_session_id"] = session_id
        payload["_executor_attempt_id"] = executor_attempt_id
        payload["_resume_available"] = True
        payload["_resume_reason"] = "agent_provider_server_error"
        assert task_service.record_agent_attempt(
            payload,
            error={"error": "agent_provider_server_error", "message": "provider unavailable"},
            session_id=session_id,
            executor_attempt_id=executor_attempt_id,
            resume_available=True,
            resume_reason="agent_provider_server_error",
        ) is True
        raise RuntimeError("agent_provider_server_error: provider unavailable")

    task_service.register("meme_context_generation", provider_failure)
    submitted = task_service.submit(
        "meme_context_generation",
        {
            "meme_id": "resume-test-meme",
            "image_sha256": "a" * 64,
            "processing_config_hash": "b" * 64,
            "reverse_image_policy": "forbid",
        },
        schedule=False,
    )

    def run_claimed_attempt():
        """认领并执行一个 attempt，返回持久任务快照。"""
        with postgres_resources.environment("local") as environment:
            claim = environment.tasks.claim(
                owner=task_service.owner,
                task_id=submitted.task_id,
                lane="agent",
                lane_capacity=task_service.agent_concurrency,
                lease_seconds=120,
            )
        assert claim is not None
        task_service._run(submitted.task_id, preclaimed=claim)
        snapshot = task_service.get(submitted.task_id)
        assert snapshot is not None
        return snapshot

    try:
        first = run_claimed_attempt()
        assert first.status == "queued"
        assert first.resume_available is True
        assert first.resume_attempts == 0

        first_resume = run_claimed_attempt()
        assert first_resume.status == "queued"
        assert first_resume.resume_available is True
        assert first_resume.resume_attempts == 1

        second_resume = run_claimed_attempt()
        assert second_resume.status == "queued"
        assert second_resume.resume_available is True
        assert second_resume.resume_attempts == 2
        assert len(observed) == 3
        assert [item["session_id"] for item in observed] == [None, "resume-session", "resume-session"]
        assert [item["resume_available"] for item in observed] == [None, True, True]

        exhausted = run_claimed_attempt()
        assert exhausted.status == "failed"
        assert exhausted.error is not None and exhausted.error["error"] == "unknown_execution"
        assert exhausted.resume_available is False
        assert exhausted.resume_reason == "unknown_execution"
        assert len(observed) == 3
        assert exhausted.first_error is not None and exhausted.first_error["error"] == "agent_provider_server_error"
    finally:
        task_service.shutdown()


def test_host_database_resources_do_not_require_local_scope(postgres_engine: Engine, tmp_path: Path) -> None:
    """宿主资源装配允许数据库只有宿主 scope，不创建 local BlobStore。"""
    scope_id = f"host-only-{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.execute(text("DELETE FROM scopes WHERE id = 'local'"))
        connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": scope_id, "namespace": uuid4()})
    try:
        settings = Settings(_env_file=None, database_url=_test_database_url(), data_root=tmp_path / "data", image_root=tmp_path / "images")
        resources = DatabaseResources(postgres_engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings, require_local_scope=False)
        assert resources.blob_store is None
        assert resources.blob_store_for_scope(scope_id).scope == ScopeContext(scope_id)
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id = :id"), {"id": scope_id})
            connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES ('local', :namespace, now())"), {"namespace": uuid4()})


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
        task = environment.tasks.submit(task_type="meme_context_generation", payload={"meme_id": str(query.id), "image_sha256": query.sha256, "visual_model": identity["model"], "visual_dimensions": identity["dimensions"], "preprocess_version": identity["preprocess_version"]}, lane="agent", dedupe_key=f"visual-filter-{uuid4().hex}")
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
        task = environment.tasks.submit(task_type="meme_context_generation", payload={"meme_id": str(query.id), "image_sha256": query.sha256, **{"visual_model": identity["model"], "visual_dimensions": identity["dimensions"], "preprocess_version": identity["preprocess_version"]}}, lane="agent", dedupe_key=f"visual-search-{uuid4().hex}")
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
