"""公共图片语境、视觉向量和 metadata repair HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
import backend.image_context_http as image_context_http
from backend.image_processing import ImageProcessingError
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError


def _request() -> SimpleNamespace:
    """构造不启动 lifespan 的最小请求对象。"""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造稳定错误 detail，模拟入口公开错误工厂。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _snapshot(job_id: str, *, stage: str = "agent", task_id: str | None = None) -> SimpleNamespace:
    """构造包含指定叶子阶段的最小图片处理 snapshot。"""
    return SimpleNamespace(
        job_id=job_id,
        status="queued",
        stages=[{"stage": stage, "task_id": task_id or f"task-{job_id}"}],
    )


def test_image_context_routes_keep_order_and_legacy_imports() -> None:
    """五个图片语境路由保持 method/status/tag/order 与旧符号。"""
    expected_paths = [
        "/images/context",
        "/images/context/batch",
        "/images/visual-embedding",
        "/images/visual-embedding/batch",
        "/images/metadata/repair",
    ]
    routes = [route for route in api.app.routes if getattr(route, "path", None) in expected_paths]
    assert [(route.path, route.methods, route.status_code) for route in routes] == [
        ("/images/context", {"POST"}, 202),
        ("/images/context/batch", {"POST"}, None),
        ("/images/visual-embedding", {"POST"}, 202),
        ("/images/visual-embedding/batch", {"POST"}, 202),
        ("/images/metadata/repair", {"POST"}, 202),
    ]
    assert all(route.tags == ["images", "tasks"] for route in routes)
    assert api.ContextRequest is image_context_http.ContextRequest
    assert api.ContextBatchRequest is image_context_http.ContextBatchRequest
    assert api.generate_context.__name__ == "generate_context"
    assert api.generate_context_batch.__name__ == "generate_context_batch"
    assert api.generate_visual_embedding.__name__ == "generate_visual_embedding"
    assert api.generate_visual_embedding_batch.__name__ == "generate_visual_embedding_batch"
    assert api.repair_metadata.__name__ == "repair_metadata"


def test_image_context_module_has_one_way_dependency_and_strict_models() -> None:
    """公共模块不依赖入口，输入模型拒绝路径和 scope 字段。"""
    source = Path(image_context_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert "api" not in imported
    assert "server_api" not in imported
    with pytest.raises(ValueError):
        image_context_http.ContextRequest.model_validate({"meme_id": "meme-1", "path": "/secret"})


def test_context_target_uses_scope_service_and_stale_sidecar_fallback() -> None:
    """单图目标只读取注入 service，sidecar 缺失时回退当前 scope Meme。"""
    request = _request()
    calls: list[object] = []
    record = SimpleNamespace(id="record-1", storage_key="image.webp")

    class Metadata:
        """模拟当前 scope metadata service。"""

        def image_for_meme(self, meme_id: str) -> object:
            """模拟 sidecar 缺失，触发数据库事实 fallback。"""
            calls.append(("image_for_meme", meme_id))
            raise MetadataError("metadata_missing")

        class blob_store:
            """提供受控图片路径解析。"""

            @staticmethod
            def resolve(storage_key: str) -> str:
                """返回当前 scope 的物理路径占位。"""
                calls.append(("resolve", storage_key))
                return "/scope/image.webp"

    @contextmanager
    def environment(received: object):
        """确认 fallback 只使用原始 Request 的 scope environment。"""
        calls.append(("environment", received))
        yield SimpleNamespace(memes=SimpleNamespace(get=lambda meme_id: record))

    result = asyncio.run(
        image_context_http.generate_context(
            request,
            image_context_http.ContextRequest(meme_id="meme-1"),
            service=lambda received, name: (calls.append(("service", received, name)) or Metadata()),
            environment=environment,
            submit_processing_job=lambda received, target, image, **kwargs: _snapshot("job-1"),
            error=_error,
        )
    )
    assert result["processing_job_id"] == "job-1"
    assert result["task_id"] == "task-job-1"
    assert set(result) == {"processing_job_id", "submission_mode", "image_stage", "job_status", "task_id", "task_type", "status"}
    assert calls[0] == ("service", request, "metadata")
    assert ("environment", request) in calls
    assert ("resolve", "image.webp") in calls


def test_context_batch_isolates_failures_and_preserves_ready_skip() -> None:
    """批量语境逐项隔离缺失、提交失败和已就绪跳过。"""
    request = _request()
    submitted: list[str] = []

    class Metadata:
        """按 meme_id 返回受控图片状态。"""

        def image_for_meme(self, meme_id: str) -> tuple[SimpleNamespace, str]:
            """让一个目标缺失，其余目标可提交。"""
            if meme_id == "missing":
                raise MetadataError("metadata_missing")
            return SimpleNamespace(id=meme_id), f"/{meme_id}.webp"

        def status(self, image: str) -> dict[str, str]:
            """让 ready 目标走 skip 分支。"""
            return {"status": "ready" if image == "/ready.webp" else "pending"}

        def create_pending(self, image: str) -> None:
            """记录 repair_required 兼容动作。"""
            del image

    def submit(_request: object, record: SimpleNamespace, _image: str, **_kwargs: object) -> SimpleNamespace:
        """让一个有效目标在入队时失败，其后目标继续提交。"""
        submitted.append(record.id)
        if record.id == "failed":
            raise RuntimeError("agent_backpressure")
        return _snapshot(f"job-{record.id}")

    payload = image_context_http.ContextBatchRequest.model_validate(
        {"items": [{"meme_id": "missing"}, {"meme_id": "ready"}, {"meme_id": "failed"}, {"meme_id": "ok"}], "include_unready": False}
    )

    result = asyncio.run(
        image_context_http.generate_context_batch(
            request,
            payload,
            service=lambda _request, _name: Metadata(),
            submit_processing_job=submit,
            error=_error,
            enqueue_error=lambda exc: "agent_backpressure" if str(exc) == "agent_backpressure" else "context_enqueue_failed",
        )
    )
    assert [item.get("error") or item.get("skipped") for item in result["results"]] == ["context_enqueue_failed", "already_ready", "agent_backpressure", None]
    assert submitted == ["failed", "ok"]
    assert set(result["results"][-1]) == {"meme_id", "processing_job_id", "submission_mode", "image_stage", "task_id", "status"}


def test_context_batch_forwards_both_processing_options_to_each_job() -> None:
    """选中图片完整重试的两项处理选项必须逐项传入 Job 提交 callback。"""
    request = _request()
    calls: list[dict[str, object]] = []

    class Metadata:
        """提供可提交的当前 scope 图片。"""

        def image_for_meme(self, meme_id: str) -> tuple[SimpleNamespace, str]:
            """按稳定 meme_id 返回目标。"""
            return SimpleNamespace(id=meme_id), f"/{meme_id}.webp"

        def status(self, _image: str) -> dict[str, str]:
            """返回未就绪状态以触发完整重试。"""
            return {"status": "pending"}

    def submit(_request: object, record: SimpleNamespace, _image: str, **kwargs: object) -> SimpleNamespace:
        """记录处理选项并返回最小 Job snapshot。"""
        calls.append({"meme_id": record.id, **kwargs})
        return _snapshot(f"job-{record.id}")

    payload = image_context_http.ContextBatchRequest.model_validate(
        {
            "items": [{"meme_id": "meme-1"}],
            "include_unready": True,
            "reverse_image_policy": "auto",
            "auto_name": True,
        }
    )
    result = asyncio.run(
        image_context_http.generate_context_batch(
            request,
            payload,
            service=lambda _request, _name: Metadata(),
            submit_processing_job=submit,
            error=_error,
            enqueue_error=lambda _exc: "context_enqueue_failed",
        )
    )

    assert result["results"][0]["meme_id"] == "meme-1"
    assert calls == [{
        "meme_id": "meme-1",
        "reverse_image_policy": "auto",
        "auto_name": True,
        "explicit_retry": True,
        "schedule": True,
    }]


def test_visual_batch_keeps_following_items_after_failure() -> None:
    """视觉批量一项失败后仍提交后续有效项。"""
    request = _request()
    submitted: list[str] = []

    class Metadata:
        """提供最小视觉目标解析。"""

        def image_for_meme(self, meme_id: str) -> tuple[SimpleNamespace, str]:
            """对 missing 返回稳定 metadata 错误。"""
            if meme_id == "missing":
                raise MetadataError("metadata_missing")
            return SimpleNamespace(id=meme_id), f"/{meme_id}.webp"

    def submit(_request: object, record: SimpleNamespace, _image: str, **_kwargs: object) -> SimpleNamespace:
        """记录实际提交项。"""
        submitted.append(record.id)
        return _snapshot(f"job-{record.id}", stage="visual")

    payload = image_context_http.ContextBatchRequest.model_validate({"items": [{"meme_id": "missing"}, {"meme_id": "ok"}], "reverse_image_policy": "auto"})
    result = asyncio.run(
        image_context_http.generate_visual_embedding_batch(
            request,
            payload,
            service=lambda _request, _name: Metadata(),
            submit_processing_job=submit,
            enqueue_error=lambda _exc: "context_enqueue_failed",
        )
    )
    assert result["results"][0] == {"meme_id": "missing", "error": "context_enqueue_failed"}
    assert result["results"][1]["task_id"] == "task-job-ok"
    assert set(result["results"][1]) == {"meme_id", "processing_job_id", "submission_mode", "image_stage", "task_id", "status"}
    assert submitted == ["ok"]


def test_visual_single_response_keeps_legacy_field_set() -> None:
    """视觉单图响应只增加旧有 task_type，不携带父 Job 专用字段。"""
    request = _request()
    metadata = SimpleNamespace(image_for_meme=lambda _meme_id: (SimpleNamespace(id="record-1"), "/image.webp"))
    result = asyncio.run(
        image_context_http.generate_visual_embedding(
            request,
            image_context_http.ContextRequest(meme_id="meme-1"),
            service=lambda _request, _name: metadata,
            submit_processing_job=lambda *_args, **_kwargs: _snapshot("job-1", stage="visual"),
            error=_error,
            enqueue_error=lambda _exc: "visual_enqueue_failed",
        )
    )
    assert set(result) == {"processing_job_id", "submission_mode", "image_stage", "task_id", "task_type", "status"}


def test_repair_metadata_uses_injected_scope_task_service() -> None:
    """metadata repair 只调用当前 scope task service 的固定任务类型。"""
    request = _request()
    calls: list[object] = []

    class Tasks:
        """记录 metadata repair 提交。"""

        def submit(self, task_type: str, payload: dict[str, object]) -> SimpleNamespace:
            """返回最小任务记录。"""
            calls.append((task_type, payload))
            return SimpleNamespace(task_id="task-repair", task_type=task_type, status="queued")

    result = asyncio.run(image_context_http.repair_metadata(request, task_service=lambda received: calls.append(received) or Tasks()))
    assert result == {"task_id": "task-repair", "task_type": "metadata_repair", "status": "queued"}
    assert calls[0] is request
    assert calls[1] == ("metadata_repair", {})


def test_api_context_wrapper_forwards_scope_callbacks() -> None:
    """入口 wrapper 必须把 scope service/environment 和 Job facade 显式注入。"""
    request = _request()
    payload = image_context_http.ContextRequest(meme_id="meme-1")
    calls: list[dict[str, object]] = []

    async def delegated(*args: object, **kwargs: object) -> dict[str, object]:
        """记录 wrapper 的 callback 注入。"""
        del args
        calls.append(kwargs)
        return {"ok": True}

    original = api._generate_context_http
    api._generate_context_http = delegated
    try:
        result = asyncio.run(api.generate_context(request, payload))
    finally:
        api._generate_context_http = original
    assert result == {"ok": True}
    assert calls[0]["service"] is api._service
    assert calls[0]["environment"] is api._environment
    assert calls[0]["submit_processing_job"] is api._submit_processing_job_for_image
    assert calls[0]["operation_error"] is api._operation_http_error


def test_context_operation_policy_uses_host_projection_callback() -> None:
    """语境 Job 的策略错误必须保留宿主 Retry-After 投影。"""
    request = _request()
    projected: list[OperationPolicyError] = []

    def operation_error(exc: OperationPolicyError) -> HTTPException:
        """构造带 Retry-After 的宿主错误。"""
        projected.append(exc)
        return HTTPException(status_code=429, detail={"error": exc.code}, headers={"Retry-After": "60"})

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_context_http.generate_context(
                request,
                image_context_http.ContextRequest(meme_id="meme-1"),
                service=lambda _request, _name: SimpleNamespace(image_for_meme=lambda _meme_id: (SimpleNamespace(id="record-1"), "/image.webp")),
                environment=lambda _request: None,
                submit_processing_job=lambda *_args, **_kwargs: (_ for _ in ()).throw(ImageProcessingError("operation_limit_exceeded", retry_at="2030-01-01T00:00:00+00:00")),
                error=_error,
                operation_error=operation_error,
            )
        )
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "60"
    assert projected[0].code == "operation_limit_exceeded"
