"""公共图片阶段 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
import backend.image_stage_http as image_stage_http
from backend.image_processing import ImageProcessingError
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError


def _request() -> SimpleNamespace:
    """构造不启动 lifespan 的最小请求对象。"""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造稳定错误 detail，模拟入口的公开错误工厂。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _snapshot(value: str, *, meme_id: str = "meme-1") -> SimpleNamespace:
    """构造只返回公开字段的 Job snapshot。"""
    return SimpleNamespace(as_dict=lambda: {"job_id": value, "meme_id": meme_id, "status": "queued"})


def test_image_stage_routes_keep_canonical_alias_snapshot_and_order() -> None:
    """图片处理静态列表、详情、重试和阶段路由保持 canonical-first 顺序。"""
    expected = [
        ("/images/processing", {"GET"}, None, True),
        ("/image-processing", {"GET"}, None, False),
        ("/images/processing/{job_id}", {"GET"}, None, True),
        ("/image-processing/{job_id}", {"GET"}, None, False),
        ("/images/processing/{job_id}/retry", {"POST"}, 202, True),
        ("/image-processing/{job_id}/retry", {"POST"}, 202, False),
        ("/images/stages", {"POST"}, 202, True),
        ("/images/processing/stages", {"POST"}, 202, False),
        ("/image-processing/stages", {"POST"}, 202, False),
        ("/images/stages/batch", {"POST"}, 202, True),
        ("/images/processing", {"POST"}, 202, True),
    ]
    paths = {item[0] for item in expected}
    routes = [route for route in api.app.routes if getattr(route, "path", None) in paths]
    actual = [(route.path, route.methods, route.status_code, route.include_in_schema) for route in routes]
    assert actual == expected
    assert all(route.tags == ["images", "tasks"] for route in routes)
    assert all(route.name in {"list_image_processing_jobs", "get_image_processing_job", "retry_image_processing_job", "submit_image_stage", "submit_image_stage_batch", "process_image_library"} for route in routes)


def test_image_stage_module_keeps_one_way_dependency_and_legacy_imports() -> None:
    """公共模块不依赖入口，旧模型和 handler 名称仍可从 api 导入。"""
    source = Path(image_stage_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "api" not in imported_modules
    assert "server_api" not in imported_modules
    assert api.ProcessingRetryRequest is image_stage_http.ProcessingRetryRequest
    assert api.ImageStageSubmissionRequest is image_stage_http.ImageStageSubmissionRequest
    assert api.ImageStageBatchItem is image_stage_http.ImageStageBatchItem
    assert api.ImageStageBatchRequest is image_stage_http.ImageStageBatchRequest
    assert api.get_image_processing_job.__name__ == "get_image_processing_job"
    assert api.list_image_processing_jobs.__name__ == "list_image_processing_jobs"
    assert api.retry_image_processing_job.__name__ == "retry_image_processing_job"
    assert api.submit_image_stage.__name__ == "submit_image_stage"
    assert api.submit_image_stage_batch.__name__ == "submit_image_stage_batch"


def test_job_reads_use_the_injected_scope_repository_once() -> None:
    """Job 详情和列表只读取 callback 返回的当前 scope repository。"""
    request = _request()
    calls: list[object] = []

    class Repository:
        """记录详情和列表调用的 scope repository。"""

        def snapshot(self, job_id: str) -> SimpleNamespace:
            """返回受控详情 snapshot。"""
            calls.append(("snapshot", job_id))
            return _snapshot(job_id)

        def list(self, *, limit: int) -> list[SimpleNamespace]:
            """返回受控列表 snapshot。"""
            calls.append(("list", limit))
            return [_snapshot("job-1")]

    repository = Repository()

    def repository_factory(received: object) -> Repository:
        """确认入口传递同一个 scope-bound request。"""
        calls.append(("factory", received))
        assert received is request
        return repository

    def service(received: object, name: str) -> object:
        """返回当前 scope 图片，并记录安全摘要所需的元数据读取。"""
        calls.append(("service", received, name))
        assert received is request
        assert name == "metadata"
        return SimpleNamespace(image_for_meme=lambda meme_id: (SimpleNamespace(id=meme_id), Path("/runtime/sample.png")))

    detail = asyncio.run(image_stage_http.get_image_processing_job(request, "job-1", service=service, error=_error, processing_repository=repository_factory))
    listing = asyncio.run(image_stage_http.list_image_processing_jobs(request, limit=7, service=service, processing_repository=repository_factory))
    expected = {"job_id": "job-1", "meme_id": "meme-1", "status": "queued", "image": {"meme_id": "meme-1", "filename": "sample.png", "saved_filename": "sample.png", "media_url": "/media/meme-1"}}
    assert detail == expected
    assert listing == {"items": [expected], "next_cursor": None}
    assert calls == [
        ("factory", request), ("snapshot", "job-1"), ("service", request, "metadata"),
        ("factory", request), ("list", 7), ("service", request, "metadata"),
    ]


def test_job_image_summary_keeps_identifier_when_image_is_unreadable() -> None:
    """图片被删除或指纹不匹配时不泄漏路径，并保留可关联的 Meme ID。"""
    request = _request()
    repository = SimpleNamespace(snapshot=lambda _job_id: _snapshot("job-1"))
    service = lambda _request, _name: SimpleNamespace(
        image_for_meme=lambda _meme_id: (_ for _ in ()).throw(MetadataError("metadata_missing"))
    )

    result = asyncio.run(
        image_stage_http.get_image_processing_job(
            request,
            "job-1",
            service=service,
            error=_error,
            processing_repository=lambda _request: repository,
        )
    )

    assert result["image"] == {"meme_id": "meme-1"}
    assert "/" not in str(result["image"])


def test_retry_creates_new_revision_without_reactivating_old_job() -> None:
    """显式重试继承旧选项、创建新 revision 并只调度新 Job。"""
    request = _request()
    calls: list[object] = []
    old = SimpleNamespace(reverse_image_policy="auto", auto_name=True, id="old-job", status="failed")
    new = SimpleNamespace(id="new-job")

    class Repository:
        """记录旧 Job 读取、新 revision 创建和 snapshot 读取。"""

        def get(self, job_id: str) -> object:
            """返回旧终态 Job。"""
            calls.append(("get", job_id))
            return old

        def retry(self, job_id: str, *, policy: object, auto_name: object, config: object) -> object:
            """返回新 revision，并确认旧 Job 仍未被改写。"""
            calls.append(("retry", job_id, policy, auto_name, config))
            assert old.status == "failed"
            assert old.id == "old-job"
            return new

        def snapshot(self, job_id: str) -> SimpleNamespace:
            """返回新 revision 的公开 snapshot。"""
            calls.append(("snapshot", job_id))
            return _snapshot(job_id)

    class Worker:
        """记录新 revision 的调度。"""

        def schedule(self, job_id: str) -> None:
            """只允许调度新 Job。"""
            calls.append(("schedule", job_id))
            assert job_id == "new-job"

    def normalize(received: object, **kwargs: object) -> SimpleNamespace:
        """记录继承后的处理选项。"""
        calls.append(("normalize", received, kwargs))
        assert received is request
        assert kwargs == {"reverse_image_policy": "auto", "auto_name": True}
        return SimpleNamespace(reverse_image_policy="auto", auto_name=True)

    result = asyncio.run(
        image_stage_http.retry_image_processing_job(
            request,
            "old-job",
            service=lambda _request, _name: SimpleNamespace(image_for_meme=lambda meme_id: (SimpleNamespace(id=meme_id), Path("sample.png"))),
            error=_error,
            processing_repository=lambda received: calls.append(("repository", received)) or Repository(),
            processing_worker=lambda received: calls.append(("worker", received)) or Worker(),
            normalize_processing_options=normalize,
            processing_config=lambda received: calls.append(("config", received)) or {"version": "new"},
        )
    )
    assert result == {"job_id": "new-job", "meme_id": "meme-1", "status": "queued", "image": {"meme_id": "meme-1", "filename": "sample.png", "saved_filename": "sample.png", "media_url": "/media/meme-1"}}
    assert [item[0] for item in calls] == ["repository", "get", "normalize", "config", "retry", "worker", "schedule", "snapshot"]


def test_retry_invalid_identifier_is_projected_as_missing_job() -> None:
    """非法或跨 scope 标识不能把 repository 异常泄漏成 500。"""
    request = _request()

    def repository_factory(_request: object) -> object:
        """模拟底层 UUID 解析拒绝。"""
        return SimpleNamespace(get=lambda _job_id: (_ for _ in ()).throw(ValueError("invalid uuid")))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_stage_http.retry_image_processing_job(
                request,
                "not-a-uuid",
                service=lambda _request, _name: None,
                error=_error,
                processing_repository=repository_factory,
                processing_worker=lambda _request: None,
                normalize_processing_options=lambda _request, **_kwargs: None,
                processing_config=lambda _request: {},
            )
        )
    assert caught.value.status_code == 404
    assert caught.value.detail["error"] == "image_processing_job_not_found"


def test_single_stage_validation_happens_before_scope_service_or_worker() -> None:
    """无效阶段在任何 metadata、配置或 Worker 副作用前被拒绝。"""
    request = _request()
    calls: list[str] = []

    def service(_request: object, _name: str) -> object:
        """记录不应发生的 metadata 读取。"""
        calls.append("service")
        return SimpleNamespace()

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_stage_http.submit_image_stage(
                request,
                image_stage_http.ImageStageSubmissionRequest(meme_id="meme-1", stage="auto_rename/../../secret"),
                service=service,
                error=_error,
                processing_worker=lambda _request: calls.append("worker") or None,
                normalize_processing_options=lambda _request, **_kwargs: calls.append("normalize") or None,
                processing_config=lambda _request: {},
                task_summary=lambda _request, _task: {},
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["error"] == "invalid_image_stage"
    assert calls == []
    with pytest.raises(ValueError):
        image_stage_http.ImageStageSubmissionRequest.model_validate({"meme_id": "meme-1", "stage": "visual", "path": "/secret"})


def test_batch_rejects_invalid_stage_before_any_side_effect() -> None:
    """批量阶段重复或包含 auto_rename 时整体拒绝。"""
    request = _request()
    calls: list[str] = []
    payload = image_stage_http.ImageStageBatchRequest.model_validate({"items": [{"meme_id": "meme-1"}], "stages": ["visual", "visual"]})

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_stage_http.submit_image_stage_batch(
                request,
                payload,
                service=lambda _request, _name: calls.append("service") or None,
                error=_error,
                processing_worker=lambda _request: calls.append("worker") or None,
                normalize_processing_options=lambda _request, **_kwargs: calls.append("normalize") or None,
                processing_config=lambda _request: {},
                task_summary=lambda _request, _task: {},
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["error"] == "invalid_image_stage"
    assert calls == []


def test_batch_isolates_partial_failures_and_counts_only_tasks() -> None:
    """单项失败不阻止后续组合，submitted_count 只统计真实 task。"""
    request = _request()
    calls: list[tuple[str, object]] = []

    class Worker:
        """对特定图片阶段注入失败的 Worker。"""

        def submit_stage(self, meme_id: str, stage: str, **kwargs: object) -> SimpleNamespace:
            """让一个组合失败，其余组合创建 task。"""
            calls.append(("worker", (meme_id, stage, kwargs)))
            if meme_id == "record-failed-worker" and stage == "visual":
                raise ImageProcessingError("target_changed")
            return SimpleNamespace(task_id=f"task-{meme_id}-{stage}", task_type=f"{stage}_task", image_stage=stage)

    class Metadata:
        """按 meme id 返回当前 scope 目标或稳定缺失错误。"""

        def image_for_meme(self, meme_id: str) -> tuple[SimpleNamespace, object]:
            """返回受控记录，或模拟跨 scope/不存在目标。"""
            calls.append(("metadata", meme_id))
            if meme_id == "missing":
                raise MetadataError("metadata_missing")
            return SimpleNamespace(id=f"record-{meme_id}"), object()

    def service(_request: object, name: str) -> Metadata:
        """返回当前 scope metadata service。"""
        assert name == "metadata"
        return Metadata()

    def normalize(_request: object, **kwargs: object) -> SimpleNamespace:
        """确认批量统一规范化两项处理选项。"""
        assert kwargs == {"reverse_image_policy": "forbid", "auto_name": False}
        return SimpleNamespace(reverse_image_policy="forbid", auto_name=False)

    def summary(_request: object, task: SimpleNamespace) -> dict[str, object]:
        """只投影安全 task id。"""
        return {"task_id": task.task_id, "task_type": task.task_type}

    payload = image_stage_http.ImageStageBatchRequest.model_validate(
        {
            "items": [{"meme_id": "ok"}, {"meme_id": "missing"}, {"meme_id": "failed-worker"}],
            "stages": ["visual", "text_embedding"],
        }
    )
    result = asyncio.run(
        image_stage_http.submit_image_stage_batch(
            request,
            payload,
            service=service,
            error=_error,
            processing_worker=lambda _request: Worker(),
            normalize_processing_options=normalize,
            processing_config=lambda _request: {"version": "test"},
            task_summary=summary,
        )
    )
    assert result["target_count"] == 6
    assert result["submitted_count"] == 3
    assert result["failed_count"] == 3
    assert [item.get("error") for item in result["results"]] == [None, None, "meme_not_found", "meme_not_found", "target_changed", None]
    assert len([item for item in calls if item[0] == "worker"]) == 4


def test_batch_forwards_processing_options_to_independent_stage_worker() -> None:
    """包含 Agent 的选中阶段重试必须保留联网策略和自动命名选项。"""
    request = _request()
    calls: list[dict[str, object]] = []

    class Worker:
        """记录独立阶段提交参数的 Worker。"""

        def submit_stage(self, meme_id: str, stage: str, **kwargs: object) -> SimpleNamespace:
            """保存目标、阶段和处理选项。"""
            calls.append({"meme_id": meme_id, "stage": stage, **kwargs})
            return SimpleNamespace(task_id=f"task-{stage}", task_type=f"{stage}_task", image_stage=stage)

    class Metadata:
        """返回当前 scope 的独立阶段目标。"""

        def image_for_meme(self, meme_id: str) -> tuple[SimpleNamespace, object]:
            """按 meme_id 返回 metadata 记录。"""
            return SimpleNamespace(id=f"record-{meme_id}"), object()

    normalize_calls: list[dict[str, object]] = []

    def normalize(_request: object, **kwargs: object) -> SimpleNamespace:
        """记录并返回规范化处理选项。"""
        normalize_calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    payload = image_stage_http.ImageStageBatchRequest.model_validate(
        {
            "items": [{"meme_id": "meme-1"}],
            "stages": ["agent", "text_embedding"],
            "reverse_image_policy": "auto",
            "auto_name": True,
        }
    )
    result = asyncio.run(
        image_stage_http.submit_image_stage_batch(
            request,
            payload,
            service=lambda _request, _name: Metadata(),
            error=_error,
            processing_worker=lambda _request: Worker(),
            normalize_processing_options=normalize,
            processing_config=lambda _request: {"version": "test"},
            task_summary=lambda _request, task: {"task_id": task.task_id},
        )
    )

    assert result["submitted_count"] == 2
    assert normalize_calls == [{"reverse_image_policy": "auto", "auto_name": True}]
    assert all(call["reverse_image_policy"] == "auto" for call in calls)
    assert all(call["auto_name"] is True for call in calls)


def test_single_stage_operation_policy_error_uses_host_projection_callback() -> None:
    """策略错误通过宿主 callback 投影并保留 retry_at，而非固化在公共模块。"""
    request = _request()
    retry_at = "2030-01-01T00:00:00+00:00"
    projection_calls: list[OperationPolicyError] = []
    worker = SimpleNamespace(
        submit_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(ImageProcessingError("operation_limit_exceeded", retry_at=retry_at))
    )

    def operation_error(exc: OperationPolicyError) -> HTTPException:
        """模拟 Server 的带 Retry-After 适配错误。"""
        projection_calls.append(exc)
        return HTTPException(status_code=429, detail={"error": exc.code}, headers={"Retry-After": "60"})

    result_service = lambda _request, _name: SimpleNamespace(image_for_meme=lambda _meme_id: (SimpleNamespace(id="record-1"), object()))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_stage_http.submit_image_stage(
                request,
                image_stage_http.ImageStageSubmissionRequest(meme_id="meme-1", stage="visual"),
                service=result_service,
                error=_error,
                processing_worker=lambda _request: worker,
                normalize_processing_options=lambda _request, **_kwargs: SimpleNamespace(reverse_image_policy="forbid"),
                processing_config=lambda _request: {},
                task_summary=lambda _request, _task: {},
                operation_error=operation_error,
            )
        )
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "60"
    assert len(projection_calls) == 1
    assert projection_calls[0].code == "operation_limit_exceeded"
    assert projection_calls[0].retry_at == retry_at


def test_api_single_stage_forwards_operation_policy_projection() -> None:
    """入口必须把 Server policy 错误投影 callback 传给公共阶段模块。"""
    request = _request()
    payload = image_stage_http.ImageStageSubmissionRequest(meme_id="meme-1", stage="agent")
    calls: list[object] = []

    async def delegated(*args: object, **kwargs: object) -> dict[str, object]:
        """记录入口注入的宿主 callback。"""
        del args
        calls.append(kwargs.get("operation_error"))
        return {"ok": True}

    original = api._submit_image_stage_http
    api._submit_image_stage_http = delegated
    try:
        result = asyncio.run(api.submit_image_stage(request, payload))
    finally:
        api._submit_image_stage_http = original
    assert result == {"ok": True}
    assert calls == [api._operation_http_error]
