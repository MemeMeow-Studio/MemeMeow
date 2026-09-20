# 在显式指定的开发 PostgreSQL 中验证反向图片诊断；每次运行保留独立测试 schema。

import asyncio
import hashlib
import io
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from loguru import logger
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema
from sqlalchemy.exc import IntegrityError

from backend.config import Settings
from backend.database import DatabaseResources
from backend.persistence.models import Base, Scope, Task
from backend.operation_diagnostics import OperationDiagnostics
from backend.reverse_image import CACHE_SCHEMA_VERSION, ReverseImageError, ReverseImageRequest, ReverseImageService, _fingerprint


@pytest.fixture(scope="module")
def resources(tmp_path_factory):
    """在已有数据库中创建独立 schema；不启动服务、不改动既有业务表或删除数据。"""
    url = os.getenv("MEMEMEOW_DIAGNOSTICS_DATABASE_URL")
    if not url:
        pytest.skip("需要显式指定开发环境 MEMEMEOW_DIAGNOSTICS_DATABASE_URL")
    schema = "diagnostics_test_" + uuid4().hex
    root = tmp_path_factory.mktemp("reverse-diagnostics")
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    admin_engine.dispose()
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    # 显式限定 schema，防止 create_all 将 public 中的旧表误认为测试表。
    Base.metadata.create_all(engine.execution_options(schema_translate_map={None: schema}))
    with Session(engine) as session:
        session.add(Scope(id="local", storage_namespace=uuid4()))
        session.commit()
    settings = Settings(
        _env_file=None, database_url=url, data_root=root / "data", image_root=root / "images",
        reverse_image_cache_root=root / "cache", reverse_image_provider="serpapi", serpapi_api_key=None,
    )
    database = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings)
    print(f"diagnostics_test_schema={schema}")
    try:
        yield database, settings
    finally:
        engine.dispose()


@pytest.fixture
def search_case(resources):
    """建立本次测试独有的运行中任务、有效图片和真实领域服务。"""
    database, settings = resources
    task_id = "diagnostics-test-" + uuid4().hex
    with Session(database.engine) as session:
        session.add(Task(
            id=task_id, scope_id="local", task_type="meme_context_generation", status="running",
            payload={"reverse_image_policy": "auto", "diagnostics_test": True},
            lease_owner=task_id, lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            claim_generation=1, attempt_count=1,
        ))
        session.commit()
    image = io.BytesIO()
    Image.new("RGB", (3, 3), color="#" + uuid4().hex[:6]).save(image, format="PNG")
    request = ReverseImageRequest(image=image.getvalue(), filename="diagnostics-test.png", task_id=task_id, request_id=uuid4().hex)
    service = ReverseImageService(settings, database)
    messages = []
    sink = logger.add(messages.append, format="{message}", filter=lambda record: record["message"].startswith("reverse_image_completed "))
    try:
        yield service, request, messages
    finally:
        logger.remove(sink)
        with Session(database.engine) as session:
            task = session.get(Task, task_id)
            task.status = "failed"
            task.completed_at = datetime.now(UTC)
            task.error = {"error": "diagnostics_test_finished"}
            session.commit()


def _event(message):
    """读取实际输出的汇总字段。"""
    return json.loads(str(message).split(" ", 1)[1])


def test_provider_unconfigured_reports_validation_failure(search_case):
    """真实缓存未命中且未配置供应商时保留稳定错误码。"""
    service, request, messages = search_case
    with pytest.raises(ReverseImageError) as raised:
        service.search(request)
    assert raised.value.code == "reverse_image_unavailable"
    event = _event(messages[0])
    assert len(messages) == 1
    assert event["error_code"] == "reverse_image_unavailable"
    assert event["error_stage"] == "validation"
    assert event["provider_called"] is False
    assert event["cache_status"] == "miss"
    assert event["lock_wait_ms"] >= 0


def test_cache_hit_and_replay_emit_distinct_facts(search_case):
    """读取有效的测试缓存并重放已保存结果，两次均不调用供应商。"""
    service, request, messages = search_case
    identity = request.normalized().identity(
        hashlib.sha256(request.image).hexdigest(), provider=service.provider_name,
        engine=service.provider_engine, cache_variant=service.provider_cache_variant,
    )
    key = _fingerprint(identity)
    now = datetime.now(UTC)
    # 写入明确的空结果测试数据，验证生产缓存读写及 usage 记录路径。
    service.cache.write(key, {
        "schema_version": CACHE_SCHEMA_VERSION,
        "snapshots": [{"fetched_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
                       "outcome": "empty", "response": {"visual_matches": []}}],
    })
    first = service.search(request)
    second = service.search(request)
    assert first == second
    assert first["cache"]["status"] == "hit"
    assert len(messages) == 2
    fresh, replay = map(_event, messages)
    assert fresh["outcome"] == "empty"
    assert fresh["cache_status"] == "hit"
    assert fresh["replay"] is False
    assert replay["replay"] is True
    assert fresh["provider_called"] is replay["provider_called"] is False
    assert "provider_ms" not in fresh
    assert fresh["persist_ms"] >= 0


def test_service_cancellation_during_cache_lock(search_case):
    """真实领域调用等待缓存锁期间取消，保持取消语义并输出当前阶段。"""
    service, request, messages = search_case
    identity = request.normalized().identity(
        hashlib.sha256(request.image).hexdigest(), provider=service.provider_name,
        engine=service.provider_engine, cache_variant=service.provider_cache_variant,
    )
    key = _fingerprint(identity)

    async def exercise():
        """持有同键锁并取消实际服务请求。"""
        async with service.cache.lock_async(key):
            task = asyncio.create_task(service.search_async(request))
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert len(messages) == 1
    event = _event(messages[0])
    assert event["outcome"] == "cancelled"
    assert event["cancel_requested"] is True
    assert event["cancel_stage"] == "lock_wait"
    assert event["lock_wait_ms"] > 0
    assert event["provider_called"] is False


def test_real_database_error_keeps_sqlstate(resources):
    """真实唯一约束冲突保留 SQLSTATE 和约束名称，不输出 SQL 与记录值。"""
    database, _settings = resources
    messages = []
    sink = logger.add(messages.append, format="{message}", filter=lambda record: record["message"].startswith("database_error_test "))
    try:
        with pytest.raises(IntegrityError):
            with OperationDiagnostics("database_error_test", phase="persist"):
                with Session(database.engine) as session:
                    session.add(Scope(id="local", storage_namespace=uuid4()))
                    session.commit()
    finally:
        logger.remove(sink)
    event = _event(messages[0])
    assert event["sqlstate"] == "23505"
    assert event["constraint_name"] == "scopes_pkey"
    assert event["error_stage"] == "persist"
    assert "INSERT" not in str(messages[0])
    assert "local" not in str(messages[0])
