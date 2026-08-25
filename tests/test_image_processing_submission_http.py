"""公共图片处理批量提交 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

import api
import backend.image_processing_submission_http as submission_http
from backend.image_processing import ImageProcessingError
from backend.metadata import MetadataError


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class _Payload(BaseModel):
    """只包含旧入口允许的图片处理选项。"""

    reverse_image_policy: str = "forbid"
    auto_name: bool = False


class _Environment:
    """提供当前 scope 分页 Meme 的最小环境夹具。"""

    def __init__(self, records: list[object]) -> None:
        self.memes = SimpleNamespace(list=lambda **_kwargs: records, count=lambda **_kwargs: len(records))

    def __enter__(self) -> "_Environment":
        """返回测试环境。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束测试环境，不吞掉异常。"""
        del exc_type, exc, traceback


class _Worker:
    """记录 submit 调用并返回指定 snapshot 的 Worker 夹具。"""

    def __init__(self, snapshots: list[object], failures: dict[str, Exception] | None = None) -> None:
        self.snapshots = iter(snapshots)
        self.failures = failures or {}
        self.calls: list[dict[str, object]] = []

    def submit(self, meme_id, sha256, **kwargs):
        """记录目标和 retry 选项，逐项返回 snapshot 或稳定异常。"""
        self.calls.append({"meme_id": meme_id, "sha256": sha256, **kwargs})
        failure = self.failures.get(str(meme_id))
        if failure is not None:
            raise failure
        return next(self.snapshots)


class _Metadata:
    """提供受控图片路径与 embedding 输入的 metadata 夹具。"""

    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[str] = []
        self.blob_store = SimpleNamespace(resolve=self.resolve)

    def resolve(self, storage_key: str):
        """按 storage key 返回受控路径或模拟读取失败。"""
        self.calls.append(storage_key)
        if storage_key in self.failures:
            raise MetadataError("image_unreadable")
        return Path("/scope") / storage_key

    def embedding_record(self, image):
        """返回当前图片 metadata hash。"""
        return {"metadata_hash": f"hash:{image.name}"}


def _request() -> SimpleNamespace:
    """构造不启动 lifespan 的最小请求对象。"""
    return SimpleNamespace(query_params={})


def _record(name: str) -> SimpleNamespace:
    """构造一个 scope 内 Meme 记录。"""
    return SimpleNamespace(id=uuid4(), storage_key=name, sha256=f"sha:{name}")


def _call(request, payload, records, worker, metadata, repository, *, normalize=None):
    """调用新模块并注入当前宿主边界。"""
    return asyncio.run(
        submission_http.process_image_library(
            request,
            payload,
            page=2,
            page_size=10,
            processing_worker=lambda _request: worker,
            normalize_processing_options=normalize or (lambda _request, **kwargs: SimpleNamespace(**kwargs)),
            processing_repository=lambda _request: repository,
            metadata_service=lambda _request: metadata,
            environment=lambda _request: _Environment(records),
            processing_config=lambda _request: {"model": "test"},
            error=_error,
        )
    )


def test_image_processing_submission_route_and_legacy_handler_remain_available() -> None:
    """canonical route metadata 与旧 handler 名称保持兼容且不重复。"""
    routes = [route for route in api.app.routes if getattr(route, "path", None) == "/images/processing" and route.methods == {"POST"}]
    assert len(routes) == 1
    assert routes[0].status_code == 202
    assert routes[0].tags == ["images", "tasks"]
    assert api.process_image_library.__name__ == "process_image_library"


def test_image_processing_submission_module_keeps_one_way_dependency() -> None:
    """图片处理提交模块不得反向导入入口模块。"""
    source = Path(submission_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported


def test_image_processing_submission_fails_before_repository_when_worker_missing() -> None:
    """Worker 不可用时先返回稳定错误，不读取图片或 repository。"""
    called: list[object] = []
    with pytest.raises(HTTPException) as caught:
        _call(_request(), _Payload(), [], None, SimpleNamespace(), SimpleNamespace(), normalize=lambda *_args, **_kwargs: called.append(True))
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "image_processing_unavailable"
    assert called == []


def test_image_processing_submission_projects_reuse_retry_and_metadata_hash() -> None:
    """成功项保留 Job 字段，旧 retryable Job 传入 explicit_retry。"""
    records = [_record("first.png"), _record("second.png")]
    latest = SimpleNamespace(job_id="old-job", status="failed")
    repository = SimpleNamespace(latest_for_target=lambda meme_id, _sha: latest if meme_id == records[0].id else None)
    snapshots = [SimpleNamespace(job_id="old-job", status="queued"), SimpleNamespace(job_id="new-job", status="running")]
    worker = _Worker(snapshots)
    metadata = _Metadata()
    payload = _Payload(reverse_image_policy="auto", auto_name=True)
    result = _call(_request(), payload, records, worker, metadata, repository)
    assert result["count"] == 2
    assert result["results"][0]["reused"] is True
    assert result["results"][0]["processing_job_id"] == "old-job"
    assert result["results"][1]["reused"] is False
    assert worker.calls[0]["explicit_retry"] is True
    assert worker.calls[0]["metadata_hash"] == "hash:first.png"
    assert worker.calls[0]["reverse_image_policy"] == "auto"


def test_image_processing_submission_isolates_image_and_worker_failures() -> None:
    """单图 metadata/Worker 失败只生成稳定结果，后续图片继续提交。"""
    records = [_record("bad.png"), _record("worker-fails.png"), _record("good.png")]
    repository = SimpleNamespace(latest_for_target=lambda *_args: None)
    worker = _Worker(
        [SimpleNamespace(job_id="good-job", status="queued")],
        failures={str(records[1].id): ImageProcessingError("processing_options_conflict")},
    )
    metadata = _Metadata({"bad.png"})
    result = _call(_request(), _Payload(), records, worker, metadata, repository)
    assert result["results"][0] == {"meme_id": str(records[0].id), "error": "image_processing_failed"}
    assert result["results"][1] == {"meme_id": str(records[1].id), "error": "processing_options_conflict"}
    assert result["results"][2]["job_id"] == "good-job"


def test_image_processing_submission_maps_option_error_before_page_read() -> None:
    """选项规范化错误在读取 Meme page 前投影稳定错误。"""
    records = [_record("image.png")]
    called: list[object] = []

    def normalize(_request, **_kwargs):
        """模拟既有选项校验失败。"""
        called.append(True)
        raise ImageProcessingError("invalid_reverse_image_policy")

    with pytest.raises(HTTPException) as caught:
        _call(_request(), _Payload(), records, _Worker([]), _Metadata(), SimpleNamespace(), normalize=normalize)
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_reverse_image_policy"
    assert called == [True]
