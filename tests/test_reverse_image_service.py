"""反向图片后端服务的 PostgreSQL 聚焦验收测试。

测试只在显式提供 ``MEMEMEOW_TEST_DATABASE_URL`` 时运行，避免误写开发数据库；
真实数据库环境下覆盖策略授权、缓存计数、请求幂等和任务终态审计。
"""

from __future__ import annotations

import io
import hashlib
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import text

from backend.config import Settings
from backend.callbacks import CallbackBinding, callback_input_digest
from backend.database import DatabaseError, DatabaseResources, StorageCoordinator, create_engine_for_url
from backend.pg_services import PostgresTaskService
from backend.reverse_image import ReverseImageError, ReverseImageRequest, ReverseImageService, _fingerprint


def _test_database_url() -> str | None:
    """读取专用集成数据库连接串，不回退到应用默认数据库。"""
    return os.getenv("MEMEMEOW_TEST_DATABASE_URL")


def _clear_database(engine: Any) -> None:
    """删除测试 scope 的业务记录，保留 schema、local scope 和安装标记。"""
    with engine.begin() as connection:
        for table in (
            "agent_callback_requests",
            "reverse_image_usage_events",
            "operation_grants",
            "image_processing_attempts",
            "image_processing_stages",
            "image_processing_jobs",
            "search_migration_states",
            "task_lane_resource_slots",
            "task_lane_slots",
            "task_batch_items",
            "task_batches",
            "tasks",
            "meme_visual_embeddings",
            "meme_embeddings",
            "search_heads",
            "search_generations",
            "storage_operations",
            "meme_collection_items",
            "meme_collections",
            "memes",
        ):
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("DELETE FROM scopes WHERE id <> 'local'"))


@pytest.fixture
def postgres_resources(tmp_path: Path):
    """提供真实 PostgreSQL 资源；连接不可用时跳过而不是触碰默认数据库。"""
    url = _test_database_url()
    if not url:
        pytest.skip("未设置 MEMEMEOW_TEST_DATABASE_URL，跳过反向图片 PostgreSQL 测试")
    engine = create_engine_for_url(url, pool_size=3, max_overflow=0)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"PostgreSQL 集成数据库不可用: {exc}")
    _clear_database(engine)
    settings = Settings(
        _env_file=None,
        database_url=url,
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        embedding_api_key="",
        embedding_base_url="",
        serpapi_api_key=None,
        opencode_model=None,
        opencode_executable=None,
    )
    resources = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings)
    try:
        yield resources, settings
    finally:
        _clear_database(engine)
        engine.dispose()


def _image_bytes(color: str = "red") -> bytes:
    """生成可被请求模型校验的最小 PNG 图片。"""
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color=color).save(output, format="PNG")
    return output.getvalue()


def _request(task_id: str, request_id: str | None = None, *, color: str = "red") -> ReverseImageRequest:
    """构造一次固定检索参数的请求，便于比较缓存身份。"""
    return ReverseImageRequest(image=_image_bytes(color), filename="meme.png", task_id=task_id, request_id=request_id)


def _running_task(resources: DatabaseResources, policy: str = "auto", *, extra: dict[str, Any] | None = None) -> tuple[str, str, int]:
    """创建并 claim 一个语境任务，返回 task_id、owner 和 claim generation。"""
    payload: dict[str, Any] = {"reverse_image_policy": policy, "image_sha256": "a" * 64}
    payload.update(extra or {})
    owner = f"reverse-test-{uuid4().hex}"
    with resources.environment("local") as environment:
        record = environment.tasks.submit(
            task_type="meme_context_generation",
            payload=payload,
            lane="default",
            dedupe_key=f"reverse-test:{uuid4().hex}",
            max_attempts=1,
        )
        claimed = environment.tasks.claim(owner=owner, task_id=record.id, lease_seconds=300)
        assert claimed is not None
        return claimed.id, owner, claimed.claim_generation


def _service(settings: Settings, resources: DatabaseResources, provider: Callable[[ReverseImageRequest], dict[str, Any]] | None = None) -> ReverseImageService:
    """构造隔离缓存根目录下的反向图片服务。"""
    return ReverseImageService(settings, resources, provider=provider)


def test_forbid_does_not_call_provider_or_hide_audit(postgres_resources):
    """forbid 在服务端拒绝调用，并保留可追溯的 forbidden 事件。"""
    resources, settings = postgres_resources
    calls: list[int] = []

    def provider(_request: ReverseImageRequest) -> dict[str, Any]:
        calls.append(1)
        return {"visual_matches": [{"title": "不应调用"}]}

    task_id, _owner, _generation = _running_task(resources, "forbid")
    with pytest.raises(ReverseImageError, match="禁止") as error:
        _service(settings, resources, provider).search(_request(task_id, "forbid-request"))
    assert error.value.code == "reverse_image_forbidden"
    assert calls == []
    with resources.environment("local") as environment:
        event = environment.reverse_image_usage.get("forbid-request")
        assert event is not None
        assert event.outcome == "forbidden"
        assert event.provider_called is False
        assert event.completed_at is not None


def test_auto_cache_miss_then_hit_counts_one_provider_call(postgres_resources):
    """相同缓存身份首次 miss、随后 hit，供应商只计一次。"""
    resources, settings = postgres_resources
    calls: list[int] = []

    def provider(_request: ReverseImageRequest) -> dict[str, Any]:
        calls.append(1)
        return {"visual_matches": [{"title": "命中候选"}]}

    task_id, _owner, _generation = _running_task(resources, "auto")
    service = _service(settings, resources, provider)
    first = service.search(_request(task_id, "cache-miss"))
    second = service.search(_request(task_id, "cache-hit"))
    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"
    assert first["provider"]["called"] is True
    assert second["provider"]["called"] is False
    assert len(calls) == 1
    with resources.environment("local") as environment:
        audit = environment.reverse_image_usage.aggregate_task(task_id)
        assert audit["attempted"] is True
        assert audit["used"] is True
        assert audit["cache_hits"] == 1
        assert audit["provider_calls"] == 1


def test_provider_failure_is_counted_and_finished(postgres_resources):
    """普通供应商异常转换为稳定错误，并保留 provider_called 失败事件。"""
    resources, settings = postgres_resources

    def provider(_request: ReverseImageRequest) -> dict[str, Any]:
        raise RuntimeError("provider-private-body")

    task_id, _owner, _generation = _running_task(resources, "auto")
    with pytest.raises(ReverseImageError) as error:
        _service(settings, resources, provider).search(_request(task_id, "failure-request"))
    assert error.value.code == "reverse_image_provider_unavailable"
    with resources.environment("local") as environment:
        event = environment.reverse_image_usage.get("failure-request")
        assert event is not None
        assert event.provider_called is True
        assert event.outcome == "failed"
        assert event.completed_at is not None
        assert "provider-private-body" not in str(event.error)


def test_request_id_idempotence_conflict_and_started_recovery(postgres_resources):
    """同 ID 重试不重复调用；跨任务或不同缓存身份使用同 ID 会冲突。"""
    resources, settings = postgres_resources
    calls: list[int] = []

    def provider(_request: ReverseImageRequest) -> dict[str, Any]:
        calls.append(1)
        return {"visual_matches": [{"title": "幂等候选"}]}

    first_task, _owner, _generation = _running_task(resources, "auto")
    second_task, _owner2, _generation2 = _running_task(resources, "auto")
    service = _service(settings, resources, provider)
    first = service.search(_request(first_task, "same-request"))
    replay = service.search(_request(first_task, "same-request"))
    assert replay["request_id"] == first["request_id"]
    assert calls == [1]
    with pytest.raises(ReverseImageError) as key_conflict:
        service.search(_request(first_task, "same-request", color="blue"))
    assert key_conflict.value.code == "usage_request_conflict"
    with pytest.raises(ReverseImageError) as task_conflict:
        service.search(_request(second_task, "same-request"))
    assert task_conflict.value.code == "usage_request_conflict"

    started_task, _owner3, _generation3 = _running_task(resources, "auto")
    started_request = _request(started_task, "started-request")
    image_sha = hashlib.sha256(started_request.image).hexdigest()
    key = _fingerprint(started_request.identity(image_sha))
    with resources.environment("local") as environment:
        event = environment.reverse_image_usage.create(request_id="started-request", task_id=started_task, meme_id=None, cache_key=key, cache_status="miss", provider="serpapi")
        environment.reverse_image_usage.mark_provider_started(event.request_id)
    # started 事件只保留已计数的未知状态，重试不能再次联系 provider。
    service = _service(settings, resources, lambda _request: pytest.fail("started event must not replay provider"))
    recovered = service.search(started_request)
    assert recovered["provider"]["called"] is True
    assert recovered["provider"]["outcome"] == "started"
    assert calls == [1]


def _callback_binding(task_id: str, owner: str, generation: int, target_sha256: str) -> CallbackBinding:
    """构造反向图片服务测试所需的当前 callback claim。"""
    return CallbackBinding(
        task_id=task_id,
        scope_id="local",
        claim_generation=generation,
        owner=owner,
        attempt=1,
        operation="analysis.reverse_image_search",
        target_sha256=target_sha256,
        issuer="mememeow",
        audience="mememeow-internal",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        nonce=f"callback-{uuid4().hex}",
    )


def test_callback_replaces_request_id_without_second_usage_grant_or_provider(postgres_resources):
    """同一规范化 refresh 输入换 ID 时恢复唯一 callback、usage、grant 和 provider。"""
    resources, settings = postgres_resources
    image = _image_bytes()
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    calls: list[str] = []

    def provider(request: ReverseImageRequest) -> dict[str, Any]:
        calls.append(request.request_id or "")
        return {"visual_matches": [{"title": "callback"}]}

    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    service = _service(settings, resources, provider)
    first = service.search(
        ReverseImageRequest(
            image=image,
            filename="meme.png",
            task_id=task_id,
            request_id="request-a",
            language=" ZH-CN ",
            query=" query ",
            refresh=True,
            source_image_sha256=meme.sha256,
            callback_binding=binding,
        )
    )
    second = service.search(
        ReverseImageRequest(
            image=image,
            filename="meme.png",
            task_id=task_id,
            request_id="request-b",
            language="zh-cn",
            query="query",
            refresh=True,
            source_image_sha256=meme.sha256,
            callback_binding=binding,
        )
    )
    assert first["request_id"] == second["request_id"] == "request-a"
    assert calls == ["request-a"]
    with resources.environment("local") as environment:
        assert len(environment.uow.session.execute(text("SELECT request_id FROM agent_callback_requests WHERE scope_id = 'local'")).all()) == 1
        assert len(environment.uow.session.execute(text("SELECT request_id FROM reverse_image_usage_events WHERE scope_id = 'local'")).all()) == 1
    assert len(service.grants._values) == 1


def test_callback_cache_hit_does_not_acquire_provider_grant(postgres_resources):
    """callback 复用有效缓存时只记录 hit usage，不创建 provider grant。"""
    resources, settings = postgres_resources
    image = _image_bytes("green")
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-hit-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    calls: list[int] = []

    def provider(_request: ReverseImageRequest) -> dict[str, Any]:
        calls.append(1)
        return {"visual_matches": [{"title": "cached"}]}

    service = _service(settings, resources, provider)
    service.search(_request(task_id, "local-cache-seed", color="green"))
    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    result = service.search(
        ReverseImageRequest(
            image=image,
            filename="meme.png",
            task_id=task_id,
            request_id="callback-cache",
            source_image_sha256=meme.sha256,
            callback_binding=binding,
        )
    )
    assert result["cache"]["status"] == "hit"
    assert result["provider"]["called"] is False
    assert calls == [1]
    assert len(service.grants._values) == 1


def test_callback_provider_started_unknown_never_replays_with_new_id(postgres_resources):
    """provider started 但结果未知时，换 ID 重试只返回稳定 unknown。"""
    resources, settings = postgres_resources
    image = _image_bytes("blue")
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-unknown-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    request = ReverseImageRequest(image=image, filename="meme.png", task_id=task_id, request_id="authoritative", refresh=True, source_image_sha256=meme.sha256, callback_binding=binding).normalized()
    image_sha = hashlib.sha256(request.image).hexdigest()
    digest = callback_input_digest(
        scope_id="local",
        task_id=task_id,
        claim_generation=generation,
        attempt=1,
        operation="analysis.reverse_image_search",
        target_sha256=meme.sha256,
        image_sha256=image_sha,
        search_type=request.search_type,
        language=request.language,
        country=request.country,
        query=request.query,
        auto_crop=request.auto_crop,
        refresh=True,
    )
    key = _fingerprint(request.identity(image_sha))
    with resources.environment("local") as environment:
        environment.callback_requests.create(request_id="authoritative", task_id=task_id, claim_generation=generation, attempt=1, operation="analysis.reverse_image_search", target_sha256=meme.sha256, input_digest=digest)
        event = environment.reverse_image_usage.create(request_id="authoritative", task_id=task_id, meme_id=meme.id, cache_key=key, cache_status="refresh", provider="serpapi", claim_generation=generation, attempt=1, operation="analysis.reverse_image_search", target_sha256=meme.sha256, input_digest=digest)
        environment.reverse_image_usage.mark_provider_started(event.request_id)
    calls: list[int] = []
    service = _service(settings, resources, lambda _request: calls.append(1) or pytest.fail("unknown callback must not replay provider"))
    with pytest.raises(ReverseImageError) as error:
        service.search(ReverseImageRequest(image=image, filename="meme.png", task_id=task_id, request_id="replacement", refresh=True, source_image_sha256=meme.sha256, callback_binding=binding))
    assert error.value.code == "reverse_image_unknown_execution"
    assert calls == []


def test_callback_missing_row_does_not_alias_existing_usage_to_new_id(postgres_resources):
    """旧 usage 已按完整绑定存在但 callback 行缺失时，不创建新权威别名。"""
    resources, settings = postgres_resources
    image = _image_bytes("orange")
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-legacy-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    request = ReverseImageRequest(
        image=image,
        filename="meme.png",
        task_id=task_id,
        request_id="replacement-legacy",
        refresh=True,
        source_image_sha256=meme.sha256,
        callback_binding=binding,
    ).normalized()
    image_sha = hashlib.sha256(request.image).hexdigest()
    digest = callback_input_digest(
        scope_id="local",
        task_id=task_id,
        claim_generation=generation,
        attempt=1,
        operation="analysis.reverse_image_search",
        target_sha256=meme.sha256,
        image_sha256=image_sha,
        search_type=request.search_type,
        language=request.language,
        country=request.country,
        query=request.query,
        auto_crop=request.auto_crop,
        refresh=request.refresh,
    )
    key = _fingerprint(request.identity(image_sha))
    with resources.environment("local") as environment:
        event = environment.reverse_image_usage.create(
            request_id="legacy-usage",
            task_id=task_id,
            meme_id=meme.id,
            cache_key=key,
            cache_status="refresh",
            provider="serpapi",
            claim_generation=generation,
            attempt=1,
            operation="analysis.reverse_image_search",
            target_sha256=meme.sha256,
            input_digest=digest,
        )
        environment.reverse_image_usage.mark_provider_started(event.request_id)

    calls: list[int] = []
    service = _service(settings, resources, lambda _request: calls.append(1) or pytest.fail("legacy usage must not replay provider"))
    with pytest.raises(ReverseImageError) as error:
        service.search(request)
    assert error.value.code == "reverse_image_unknown_execution"
    assert calls == []
    with resources.environment("local") as environment:
        assert environment.callback_requests.get("replacement-legacy") is None


def test_callback_unknown_row_reconciles_when_usage_already_completed(postgres_resources):
    """callback 写成 unknown 后 usage 已有明确终态时，只补写 callback 并恢复结果。"""
    resources, settings = postgres_resources
    image = _image_bytes("yellow")
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-reconcile-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    request = ReverseImageRequest(
        image=image,
        filename="meme.png",
        task_id=task_id,
        request_id="authoritative-reconcile",
        refresh=True,
        source_image_sha256=meme.sha256,
        callback_binding=binding,
    ).normalized()
    image_sha = hashlib.sha256(request.image).hexdigest()
    digest = callback_input_digest(
        scope_id="local",
        task_id=task_id,
        claim_generation=generation,
        attempt=1,
        operation="analysis.reverse_image_search",
        target_sha256=meme.sha256,
        image_sha256=image_sha,
        search_type=request.search_type,
        language=request.language,
        country=request.country,
        query=request.query,
        auto_crop=request.auto_crop,
        refresh=request.refresh,
    )
    key = _fingerprint(request.identity(image_sha))
    with resources.environment("local") as environment:
        callback = environment.callback_requests.create(
            request_id="authoritative-reconcile",
            task_id=task_id,
            claim_generation=generation,
            attempt=1,
            operation="analysis.reverse_image_search",
            target_sha256=meme.sha256,
            input_digest=digest,
        )
        event = environment.reverse_image_usage.create(
            request_id=callback.request_id,
            task_id=task_id,
            meme_id=meme.id,
            cache_key=key,
            cache_status="refresh",
            provider="serpapi",
            claim_generation=generation,
            attempt=1,
            operation="analysis.reverse_image_search",
            target_sha256=meme.sha256,
            input_digest=digest,
        )
        environment.reverse_image_usage.finish(
            event.request_id,
            outcome="success",
            result={"used": True, "snapshot": {"outcome": "success", "response": {"visual_matches": []}}},
        )
        environment.callback_requests.finish(
            callback.request_id,
            state="unknown_execution",
            error={"error": "reverse_image_unknown_execution"},
        )

    calls: list[int] = []
    result = _service(settings, resources, lambda _request: calls.append(1) or pytest.fail("completed usage must not replay provider")).search(
        ReverseImageRequest(
            image=image,
            filename="meme.png",
            task_id=task_id,
            request_id="replacement-reconcile",
            refresh=True,
            source_image_sha256=meme.sha256,
            callback_binding=binding,
        )
    )
    assert result["request_id"] == "authoritative-reconcile"
    assert result["provider"]["called"] is False
    assert calls == []
    with resources.environment("local") as environment:
        recovered = environment.callback_requests.get("authoritative-reconcile")
        assert recovered is not None and recovered.state == "completed"


def test_callback_unknown_usage_terminal_unknown_stays_unknown(postgres_resources):
    """usage 终态携带未知错误时，callback 不能被误收束为普通失败。"""
    resources, settings = postgres_resources
    image = _image_bytes("pink")
    meme = StorageCoordinator(resources).upload(image, target_key=f"callback-terminal-unknown-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    task_id, owner, generation = _running_task(resources, "auto", extra={"meme_id": str(meme.id), "image_sha256": meme.sha256})
    binding = _callback_binding(task_id, owner, generation, meme.sha256)
    request = ReverseImageRequest(image=image, filename="meme.png", task_id=task_id, request_id="terminal-unknown", refresh=True, source_image_sha256=meme.sha256, callback_binding=binding).normalized()
    image_sha = hashlib.sha256(request.image).hexdigest()
    digest = callback_input_digest(
        scope_id="local",
        task_id=task_id,
        claim_generation=generation,
        attempt=1,
        operation="analysis.reverse_image_search",
        target_sha256=meme.sha256,
        image_sha256=image_sha,
        search_type=request.search_type,
        language=request.language,
        country=request.country,
        query=request.query,
        auto_crop=request.auto_crop,
        refresh=request.refresh,
    )
    key = _fingerprint(request.identity(image_sha))
    with resources.environment("local") as environment:
        callback = environment.callback_requests.create(request_id="terminal-unknown", task_id=task_id, claim_generation=generation, attempt=1, operation="analysis.reverse_image_search", target_sha256=meme.sha256, input_digest=digest)
        event = environment.reverse_image_usage.create(request_id=callback.request_id, task_id=task_id, meme_id=meme.id, cache_key=key, cache_status="refresh", provider="serpapi", claim_generation=generation, attempt=1, operation="analysis.reverse_image_search", target_sha256=meme.sha256, input_digest=digest)
        environment.reverse_image_usage.finish(event.request_id, outcome="failed", error={"error": "reverse_image_unknown_execution"})
        environment.callback_requests.finish(callback.request_id, state="unknown_execution", error={"error": "reverse_image_unknown_execution"})

    with pytest.raises(ReverseImageError) as error:
        _service(settings, resources, lambda _request: pytest.fail("unknown usage must not replay provider")).search(
            ReverseImageRequest(image=image, filename="meme.png", task_id=task_id, request_id="replacement-terminal-unknown", refresh=True, source_image_sha256=meme.sha256, callback_binding=binding)
        )
    assert error.value.code == "reverse_image_unknown_execution"


def test_callback_usage_binding_rejects_terminal_fact_rebinding(postgres_resources):
    """usage 已有终态时，改 Task/attempt/digest 仍沿用完整绑定冲突。"""
    resources, _settings = postgres_resources
    task_id, _owner, generation = _running_task(resources, "auto")
    request_id = "binding-terminal"
    with resources.environment("local") as environment:
        event = environment.reverse_image_usage.create(
            request_id=request_id,
            task_id=task_id,
            meme_id=None,
            cache_key="a" * 64,
            cache_status="miss",
            claim_generation=generation,
            attempt=1,
            operation="analysis.reverse_image_search",
            target_sha256="b" * 64,
            input_digest="c" * 64,
        )
        environment.reverse_image_usage.finish(event.request_id, outcome="failed", error={"error": "reverse_image_provider_unavailable"})
        with pytest.raises(DatabaseError, match="usage_request_conflict"):
            environment.reverse_image_usage.create(
                request_id=request_id,
                task_id=task_id,
                meme_id=None,
                cache_key="a" * 64,
                cache_status="miss",
                claim_generation=generation,
                attempt=2,
                operation="analysis.reverse_image_search",
                target_sha256="b" * 64,
                input_digest="c" * 64,
            )


def test_same_image_different_policy_is_rejected(postgres_resources):
    """活动图片任务的策略变化必须报冲突，而不是静默复用另一策略。"""
    resources, settings = postgres_resources
    task_service = PostgresTaskService(resources, agent_concurrency=1, max_attempts=1)
    payload = {"meme_id": "same-meme", "image_sha256": "b" * 64, "reverse_image_policy": "auto"}
    try:
        first = task_service.submit("meme_context_generation", payload, schedule=False)
        assert first.payload["reverse_image_policy"] == "auto"
        conflict = dict(payload)
        conflict["reverse_image_policy"] = "forbid"
        with pytest.raises(RuntimeError, match="generation_policy_conflict"):
            task_service.submit("meme_context_generation", conflict, schedule=False)
    finally:
        task_service.shutdown()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (("policy", "reverse_image_forbidden"), ("status", "task_not_running")),
)
def test_cache_lock_rechecks_task_policy_and_status(postgres_resources, mutation: str, expected: str):
    """缓存锁等待后重新读取授权状态，不能只依赖锁外 running 快照。"""
    resources, settings = postgres_resources
    task_id, _owner, _generation = _running_task(resources, "auto")
    service = _service(settings, resources, lambda _request: {"visual_matches": []})
    original_lock = service.cache.lock

    @contextmanager
    def mutating_lock(key: str):
        with original_lock(key):
            with resources.environment("local") as environment:
                task = environment.tasks.get(task_id, for_update=True)
                assert task is not None
                if mutation == "policy":
                    task.payload = {**(task.payload or {}), "reverse_image_policy": "forbid"}
                else:
                    task.status = "failed"
                environment.uow.session.flush()
            yield

    service.cache.lock = mutating_lock  # type: ignore[method-assign]
    with pytest.raises(ReverseImageError) as error:
        service.search(_request(task_id, f"lock-{mutation}"))
    assert error.value.code == expected


def _wait_terminal(task_service: PostgresTaskService, task_id: str):
    """等待异步任务进入终态，失败时返回最后一份任务快照。"""
    deadline = time.monotonic() + 8
    record = task_service.get(task_id)
    while record is not None and record.status not in {"succeeded", "failed"} and time.monotonic() < deadline:
        time.sleep(0.05)
        record = task_service.get(task_id)
    assert record is not None
    assert record.status in {"succeeded", "failed"}
    return record


def test_task_success_and_failure_keep_terminal_audit_and_provenance(postgres_resources):
    """任务服务把成功/失败 usage 摘要写入终态，成功才更新 Meme provenance。"""
    resources, settings = postgres_resources
    image = _image_bytes()
    meme = StorageCoordinator(resources).upload(image, target_key=f"audit-{uuid4().hex}.png", extension=".png", context={}, provenance={})
    success_calls: list[int] = []

    def success_provider(_request: ReverseImageRequest) -> dict[str, Any]:
        success_calls.append(1)
        return {"visual_matches": [{"title": "审计成功"}]}

    success_service = _service(settings, resources, success_provider)
    worker = PostgresTaskService(resources, agent_concurrency=1, max_attempts=1)

    def success_handler(payload: dict[str, Any], _progress):
        return success_service.search(ReverseImageRequest(image=image, filename="meme.png", task_id=str(payload["_claim_task_id"]), request_id=str(payload["request_id"])))

    worker.register("meme_context_generation", success_handler)
    success_task = worker.submit(
        "meme_context_generation",
        {"meme_id": str(meme.id), "image_sha256": meme.sha256, "reverse_image_policy": "auto", "request_id": "terminal-success"},
    )
    try:
        succeeded = _wait_terminal(worker, success_task.task_id)
        assert succeeded.status == "succeeded"
        assert succeeded.result["reverse_image"]["outcome"] == "success"
        assert succeeded.result["reverse_image"]["provider_calls"] == 1
        with resources.environment("local") as environment:
            stored = environment.memes.get(meme.id)
            assert stored is not None
            assert stored.provenance["reverse_image"]["outcome"] == "success"
        assert success_calls == [1]
    finally:
        worker.shutdown()

    def failure_provider(_request: ReverseImageRequest) -> dict[str, Any]:
        raise RuntimeError("failure-body")

    failure_service = _service(settings, resources, failure_provider)
    failing_worker = PostgresTaskService(resources, agent_concurrency=1, max_attempts=1)

    def failure_handler(payload: dict[str, Any], _progress):
        return failure_service.search(ReverseImageRequest(image=image, filename="meme.png", task_id=str(payload["_claim_task_id"]), request_id=str(payload["request_id"]), refresh=True))

    failing_worker.register("meme_context_generation", failure_handler)
    failed_task = failing_worker.submit(
        "meme_context_generation",
        {"meme_id": str(meme.id), "image_sha256": meme.sha256, "reverse_image_policy": "auto", "request_id": "terminal-failure"},
    )
    try:
        failed = _wait_terminal(failing_worker, failed_task.task_id)
        assert failed.status == "failed"
        assert failed.result["reverse_image"]["outcome"] == "failed"
        assert failed.result["reverse_image"]["provider_calls"] == 1
        with resources.environment("local") as environment:
            stored = environment.memes.get(meme.id)
            assert stored is not None
            assert "reverse_image" in stored.provenance
            assert stored.provenance["reverse_image"]["outcome"] == "success"
    finally:
        failing_worker.shutdown()


def test_usage_events_are_scope_isolated(postgres_resources):
    """不同 scope 的任务、事件和聚合互不可见。"""
    resources, _settings = postgres_resources
    other_scope = f"reverse-scope-{uuid4().hex}"
    with resources.engine.begin() as connection:
        connection.execute(text("INSERT INTO scopes(id, storage_namespace, created_at) VALUES (:id, :namespace, now())"), {"id": other_scope, "namespace": uuid4()})
    try:
        with resources.environment("local") as environment:
            local_task = environment.tasks.submit(task_type="meme_context_generation", payload={"reverse_image_policy": "auto"}, lane="default", dedupe_key=f"local-{uuid4().hex}")
            local_key = "1" * 64
            environment.reverse_image_usage.create(request_id="scope-local-request", task_id=local_task.id, meme_id=None, cache_key=local_key, cache_status="miss")
            environment.reverse_image_usage.finish("scope-local-request", outcome="forbidden", result={"used": False})
        with resources.environment(other_scope) as environment:
            other_task = environment.tasks.submit(task_type="meme_context_generation", payload={"reverse_image_policy": "auto"}, lane="default", dedupe_key=f"other-{uuid4().hex}")
            other_key = "2" * 64
            environment.reverse_image_usage.create(request_id="scope-other-request", task_id=other_task.id, meme_id=None, cache_key=other_key, cache_status="miss")
            environment.reverse_image_usage.finish("scope-other-request", outcome="success", result={"used": True})
            with pytest.raises(DatabaseError, match="usage_event_conflict"):
                environment.reverse_image_usage.create(request_id="scope-local-request", task_id=other_task.id, meme_id=None, cache_key=other_key, cache_status="miss")
        with resources.environment("local") as environment:
            assert environment.reverse_image_usage.get("scope-other-request") is None
            assert environment.reverse_image_usage.aggregate_task(local_task.id)["request_count"] == 1
        with resources.environment(other_scope) as environment:
            assert environment.reverse_image_usage.get("scope-local-request") is None
            assert environment.reverse_image_usage.aggregate_task(other_task.id)["request_count"] == 1
    finally:
        with resources.engine.begin() as connection:
            connection.execute(text("DELETE FROM scopes WHERE id = :id"), {"id": other_scope})
