"""视觉候选 snapshot 的版本、排序、深拷贝和公开摘要契约测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from backend.tasks import TaskRecord
from backend.persistence.engine import DatabaseError
from backend.persistence.models import ScopeContext
from backend.persistence.repositories.tasks import TaskRepository
from backend.visual_snapshot import (
    VisualMatchSnapshotError,
    build_visual_match_snapshot,
    validate_visual_match_snapshot,
    visual_match_snapshot_manifest,
    visual_match_snapshot_summary,
)
from backend.services.tasks import PostgresTaskService


def _snapshot() -> dict[str, object]:
    """构造包含相同分数候选的最小合法 snapshot。"""
    return build_visual_match_snapshot(
        query_meme_id="query",
        image_sha256="a" * 64,
        model="dinov2_vitb14",
        dimensions=768,
        preprocess_version="dinov2-v1-gif-first-frame",
        candidates=[
            {
                "meme_id": "meme-b",
                "image_sha256": "b" * 64,
                "size_bytes": 12,
                "score": 0.8,
                "relative_path": "candidate-02.png",
                "context": {"title": "B"},
            },
            {
                "meme_id": "meme-a",
                "image_sha256": "c" * 64,
                "size_bytes": 10,
                "score": 0.8,
                "relative_path": "candidate-01.png",
                "context": {"title": "A"},
            },
        ],
        matched_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


def test_snapshot_has_stable_order_hash_and_deep_copied_context() -> None:
    """候选按分数和 ID 稳定排序，context 修改不影响已生成 snapshot。"""
    snapshot = _snapshot()
    candidates = snapshot["candidates"]
    assert isinstance(candidates, list)
    assert [item["meme_id"] for item in candidates] == ["meme-a", "meme-b"]
    digest = snapshot["snapshot_sha256"]
    assert isinstance(digest, str) and len(digest) == 64

    validated = validate_visual_match_snapshot(snapshot, expected_sha256=digest)
    assert validated == snapshot
    snapshot["candidates"][0]["context"]["title"] = "changed"  # type: ignore[index]
    assert validated["candidates"][0]["context"]["title"] == "A"  # type: ignore[index]


def test_snapshot_summary_excludes_candidate_facts() -> None:
    """公开摘要只包含版本、hash、时间和数量，不泄露候选语境或路径。"""
    summary = visual_match_snapshot_summary(_snapshot())
    assert set(summary) == {"protocol_version", "snapshot_sha256", "matched_at", "candidate_count"}
    assert summary["candidate_count"] == 2

    record = TaskRecord(
        task_id="task",
        task_type="meme_context_generation",
        visual_snapshot_sha256=summary["snapshot_sha256"],
        visual_snapshot_protocol_version=summary["protocol_version"],
        visual_snapshot_matched_at=summary["matched_at"],
        visual_snapshot_candidate_count=summary["candidate_count"],
        result={"visual_match_snapshot": {**summary, "context": {"secret": "no"}}},
    )
    public = record.as_dict()
    assert public["visual_match_snapshot"] == summary
    assert "context" not in public["visual_match_snapshot"]


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda value: value["candidates"].append(dict(value["candidates"][0])), "visual_match_snapshot_invalid"),
        (lambda value: value["candidates"][0].update({"score": float("nan")}), "visual_match_snapshot_invalid"),
        (lambda value: value["candidates"][0].update({"relative_path": "../outside.png"}), "visual_match_snapshot_invalid"),
    ],
)
def test_snapshot_validation_rejects_corruption(mutator, code: str) -> None:
    """重复候选、非 finite 分数和路径跳转不能进入任务事实。"""
    snapshot = _snapshot()
    mutator(snapshot)
    with pytest.raises(VisualMatchSnapshotError) as captured:
        validate_visual_match_snapshot(snapshot)
    assert captured.value.code == code


def test_snapshot_expected_hash_mismatch_is_fail_closed() -> None:
    """恢复时摘要 hash 改变必须拒绝，而不能静默重算。"""
    with pytest.raises(VisualMatchSnapshotError) as captured:
        validate_visual_match_snapshot(_snapshot(), expected_sha256="f" * 64)
    assert captured.value.code == "visual_match_snapshot_invalid"


def test_task_repository_rejects_partial_snapshot_summary() -> None:
    """JSONB 缺失但摘要列残留时不能被当作未预计算任务。"""
    task = SimpleNamespace(
        id="task",
        task_type="meme_context_generation",
        visual_match_snapshot=None,
        visual_snapshot_sha256="a" * 64,
        visual_snapshot_protocol_version=2,
        visual_snapshot_matched_at=None,
        visual_snapshot_candidate_count=None,
    )

    class Session:
        """返回固定 Task 的最小 repository session。"""

        def scalar(self, _statement):
            """返回测试任务。"""
            return task

    repository = TaskRepository(Session(), ScopeContext("local"))
    with pytest.raises(DatabaseError) as captured:
        repository.get_visual_snapshot("task")
    assert captured.value.code == "visual_match_snapshot_invalid"


def test_snapshot_builder_drops_manifest_unsafe_context_fields() -> None:
    """snapshot 只保留研究语境，不能把 URL、storage key 或 scope 扩展带入 manifest。"""
    snapshot = build_visual_match_snapshot(
        query_meme_id="query",
        image_sha256="a" * 64,
        model="dinov2_vitb14",
        dimensions=768,
        preprocess_version="dinov2-v1-gif-first-frame",
        candidates=[
            {
                "meme_id": "meme-a",
                "image_sha256": "b" * 64,
                "size_bytes": 1,
                "score": 0.5,
                "relative_path": "candidate-01.png",
                "context": {
                    "title": "参考",
                    "source_urls": ["https://internal.example/source"],
                    "storage_key": "secret-storage-key",
                    "scope_id": "scope-secret",
                },
            }
        ],
        matched_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    manifest = visual_match_snapshot_manifest(snapshot)
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert manifest["candidates"][0]["context"] == {"title": "参考"}  # type: ignore[index]
    assert "https://" not in serialized
    assert "storage_key" not in serialized
    assert "scope_id" not in serialized


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value["query"].__setitem__("scope_id", "scope-secret"),
        lambda value: value["candidates"][0].__setitem__("storage_key", "secret"),
        lambda value: value["candidates"][0]["context"].__setitem__("scope_id", "scope-secret"),
    ],
)
def test_snapshot_validation_rejects_unknown_fields(mutator) -> None:
    """snapshot、query、候选和语境中的未知字段都不能伪装成同一事实。"""
    snapshot = _snapshot()
    mutator(snapshot)
    with pytest.raises(VisualMatchSnapshotError) as captured:
        validate_visual_match_snapshot(snapshot)
    assert captured.value.code == "visual_match_snapshot_invalid"


def test_task_service_prepares_snapshot_once_and_reuses_it() -> None:
    """任务前置器首次保存 snapshot，后续 claim 不再次调用视觉匹配。"""
    snapshot = _snapshot()

    class Tasks:
        """提供 snapshot repository 最小 fencing 夹具。"""

        def __init__(self) -> None:
            """初始化空的任务 snapshot。"""
            self.value = None

        def get_visual_snapshot(self, _task_id: str):
            """返回当前保存的 snapshot。"""
            return self.value

        def set_visual_snapshot_fenced(self, _task_id: str, _generation: int, _owner: str, value):
            """记录一次成功的 claim fenced snapshot 写入。"""
            self.value = value
            return True

        def interrupt_owner(self, _owner: str) -> None:
            """测试夹具不持有真实租约，因此无需中断任务。"""

    class Environment:
        """提供共享 tasks repository 的事务上下文。"""

        def __init__(self, tasks: Tasks) -> None:
            """保存任务夹具。"""
            self.tasks = tasks

        def __enter__(self):
            """进入测试事务。"""
            return self

        def __exit__(self, *_args) -> bool:
            """结束测试事务。"""
            return False

    class Resources:
        """把所有 scope environment 指向同一个测试 repository。"""

        def __init__(self) -> None:
            """初始化任务夹具。"""
            self.tasks = Tasks()

        def environment(self, _scope: str):
            """返回 scope-bound 测试环境。"""
            return Environment(self.tasks)

    calls = []
    resources = Resources()
    service = PostgresTaskService(
        resources,
        visual_snapshot_preparer=lambda **_kwargs: (calls.append(True) or snapshot),
    )
    try:
        claim = SimpleNamespace(id="task", task_type="meme_context_generation", claim_generation=1)
        payload = {
            "meme_id": "query",
            "image_sha256": "a" * 64,
            "visual_model": "dinov2_vitb14",
            "visual_dimensions": 768,
            "preprocess_version": "dinov2-v1-gif-first-frame",
            "visual_match_snapshot_protocol_version": 2,
        }
        service._prepare_visual_snapshot(claim, payload)
        service._prepare_visual_snapshot(claim, payload)
        assert len(calls) == 1
        assert payload["_visual_snapshot_sha256"] == snapshot["snapshot_sha256"]
        assert payload["_visual_snapshot_candidate_count"] == 2
    finally:
        service.shutdown()


def test_task_service_resume_without_snapshot_never_rematches() -> None:
    """protocol v2 恢复缺失 snapshot 时必须稳定失败且不调用视觉前置器。"""
    class Tasks:
        """提供缺失 snapshot 的恢复夹具。"""

        def get_visual_snapshot(self, _task_id: str):
            """模拟持久层没有 snapshot。"""
            return None

        def interrupt_owner(self, _owner: str) -> None:
            """测试夹具不持有真实租约，因此无需中断任务。"""

    class Environment:
        """提供 scope 事务上下文。"""

        def __init__(self) -> None:
            """初始化任务 repository。"""
            self.tasks = Tasks()

        def __enter__(self):
            """进入测试事务。"""
            return self

        def __exit__(self, *_args) -> bool:
            """结束测试事务。"""
            return False

    class Resources:
        """提供固定环境。"""

        def environment(self, _scope: str):
            """返回测试事务。"""
            return Environment()

    calls: list[str] = []
    service = PostgresTaskService(Resources(), visual_snapshot_preparer=lambda **_kwargs: calls.append("match"))
    try:
        claim = SimpleNamespace(id="task", task_type="meme_context_generation", claim_generation=1)
        payload = {
            "meme_id": "query",
            "image_sha256": "a" * 64,
            "visual_match_snapshot_protocol_version": 2,
            "_resume_available": True,
        }
        with pytest.raises(RuntimeError, match="visual_match_snapshot_invalid"):
            service._prepare_visual_snapshot(claim, payload)
        assert calls == []
    finally:
        service.shutdown()


def _stub_agent_task_service(events: list[object]) -> tuple[PostgresTaskService, SimpleNamespace]:
    """构造只覆盖任务顺序的无数据库 service 夹具。"""
    service = object.__new__(PostgresTaskService)
    service.scope = SimpleNamespace(scope_id="local")
    service.owner = "test-owner"
    service.resume_enabled = False
    service.lease_seconds = 120
    service.resume_max_backoff_seconds = 60
    service.resume_backoff_seconds = 0
    service._worker_manager = None
    service._handlers = {}
    service._stopped = Event()
    service._stopped.set()
    service._scheduled = set()
    service._lock = Lock()
    service._image_attempt_state = lambda _claim, _payload, state: events.append(f"attempt:{state}")
    service._fenced_update = lambda *_args, **_kwargs: True
    service._with_reverse_image_audit = lambda _task_id, result, **_kwargs: result or {}
    service._fenced_success = lambda *_args, **_kwargs: events.append("success") or True
    service._fenced_failure = lambda *_args, **kwargs: events.append(f"failure:{kwargs['error']['error']}") or True
    service._maybe_finalize = lambda *_args, **_kwargs: None
    claim = SimpleNamespace(
        id="task",
        scope_id="local",
        task_type="meme_context_generation",
        submission_mode="standalone",
        image_stage="agent",
        claim_generation=1,
        attempt_count=1,
        resume_attempt_count=0,
        resume_started_at=None,
        payload={
            "meme_id": "query",
            "image_sha256": "a" * 64,
            "visual_model": "dinov2_vitb14",
            "visual_dimensions": 768,
            "preprocess_version": "dinov2-v1-gif-first-frame",
            "visual_match_snapshot_protocol_version": 2,
        },
    )
    return service, claim


def test_task_service_materializes_before_grant_and_external_window() -> None:
    """成功 Agent 顺序必须是 snapshot、物化、grant、external_started、handler。"""
    events: list[object] = []
    service, claim = _stub_agent_task_service(events)
    service._prepare_visual_snapshot = lambda *_args: events.append("snapshot")
    service._visual_candidate_preparer = object()
    service._commit_agent_grant = lambda *_args: events.append("grant")
    service._handlers["meme_context_generation"] = lambda _payload, _progress: events.append("handler") or {}

    service._run("task", preclaimed=claim)

    assert events == [
        "attempt:prepared",
        "snapshot",
        "attempt:prepared",
        "grant",
        "attempt:grant_committed",
        "attempt:external_started",
        "handler",
        "attempt:completed",
        "success",
    ]


def test_task_service_precompute_failure_does_not_commit_grant() -> None:
    """预计算稳定失败只能收束当前任务，不能进入 grant 或 handler。"""
    events: list[object] = []
    service, claim = _stub_agent_task_service(events)
    service._prepare_visual_snapshot = lambda *_args: (_ for _ in ()).throw(RuntimeError("query_embedding_not_ready"))
    service._commit_agent_grant = lambda *_args: events.append("grant")
    service._handlers["meme_context_generation"] = lambda _payload, _progress: events.append("handler") or {}

    service._run("task", preclaimed=claim)

    assert "grant" not in events
    assert "handler" not in events
    assert "failure:query_embedding_not_ready" in events
