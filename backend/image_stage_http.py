"""公共核心图片处理 Job 与独立阶段 HTTP 边界。

本模块负责图片处理 Job 的读取/重试和独立阶段提交。scope-bound service、repository、
Worker、配置、错误、任务摘要及宿主 operation policy 投影由入口通过 callback 注入，
不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from backend.image_processing import ImageProcessingError, ImageProcessingWorker
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError


class _StrictRequestModel(BaseModel):
    """图片阶段 JSON 请求基类，拒绝客户端提交未定义字段。"""

    model_config = ConfigDict(extra="forbid")


class ProcessingRetryRequest(_StrictRequestModel):
    """图片处理 Job 的显式重试请求；重试策略由服务端规范化。"""

    reverse_image_policy: str | None = Field(default=None, pattern="^(forbid|auto)$")
    auto_name: StrictBool | None = None


class ImageStageSubmissionRequest(_StrictRequestModel):
    """受限独立图片阶段提交请求；目标输入由当前 scope 的 Meme 派生。"""

    meme_id: str = Field(min_length=1, max_length=255)
    stage: str = Field(min_length=1, max_length=64)
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")


class ImageStageBatchItem(_StrictRequestModel):
    """图片库批量阶段提交中的单张图片标识。"""

    meme_id: str = Field(min_length=1, max_length=255)


class ImageStageBatchRequest(_StrictRequestModel):
    """图片库批量阶段提交请求，只允许三个核心阶段和统一处理选项。"""

    items: list[ImageStageBatchItem] = Field(default_factory=list, max_length=500)
    stages: list[str] = Field(min_length=1, max_length=3)
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")
    auto_name: StrictBool = False


Service = Callable[[Request, str], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]
ProcessingRepository = Callable[[Request], Any]
ProcessingWorker = Callable[[Request], Any]
NormalizeOptions = Callable[..., Any]
ProcessingConfig = Callable[[Request], Mapping[str, object]]
TaskSummary = Callable[[Request, Any], dict[str, object]]
OperationError = Callable[[OperationPolicyError], HTTPException]


def _stage_error(
    exc: ImageProcessingError,
    *,
    error: ErrorFactory,
    operation_error: OperationError | None,
) -> HTTPException:
    """把阶段 Worker 错误映射为稳定 HTTP 异常，并允许宿主覆盖策略投影。

    输入是图片处理领域错误；输出只包含公开 error code/message。Server 可通过
    ``operation_error`` callback 添加 Retry-After 等适配层响应头。
    """
    if exc.code in {"operation_forbidden", "operation_limit_exceeded", "operation_policy_unavailable"} and operation_error is not None:
        return operation_error(OperationPolicyError(exc.code, retry_at=exc.retry_at))
    status = 422 if exc.code == "invalid_image_stage" else 404 if exc.code == "target_changed" else 403 if exc.code == "operation_forbidden" else 429 if exc.code == "operation_limit_exceeded" else 503 if exc.code in {"operation_policy_unavailable", "image_processing_unavailable"} else 409
    return error(status, exc.code, "独立图片阶段提交失败")


async def get_image_processing_job(
    request: Request,
    job_id: str,
    *,
    error: ErrorFactory,
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """按当前 scope 查询逐图处理 Job，并返回安全 snapshot。

    Job 不存在、标识跨 scope 或输入无法转换时统一返回
    ``404/image_processing_job_not_found``，调用场景是 canonical 和 legacy 详情路由。
    """
    try:
        snapshot = processing_repository(request).snapshot(job_id)
    except (TypeError, ValueError):
        snapshot = None
    if snapshot is None:
        raise error(404, "image_processing_job_not_found", "图片处理任务不存在")
    return snapshot.as_dict()


async def list_image_processing_jobs(
    request: Request,
    *,
    limit: int = Query(default=50, ge=1, le=200),
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """列出当前 scope 的完整 pipeline Job。

    输入是受 FastAPI 限制的 limit；输出包含安全 snapshot 列表和兼容的空 next_cursor。
    """
    repository = processing_repository(request)
    snapshots = repository.list(limit=limit)
    return {"items": [snapshot.as_dict() for snapshot in snapshots], "next_cursor": None}


async def retry_image_processing_job(
    request: Request,
    job_id: str,
    payload: ProcessingRetryRequest | None = None,
    *,
    error: ErrorFactory,
    processing_repository: ProcessingRepository,
    processing_worker: ProcessingWorker,
    normalize_processing_options: NormalizeOptions,
    processing_config: ProcessingConfig,
) -> dict[str, object]:
    """显式创建新的图片处理 Job revision，不重新激活旧 Job。

    重试策略和配置由 callback 规范化，成功后仅在当前 scope 的 Worker 可用时调度新
    revision；返回值来自 repository 的公开 snapshot。
    """
    repository = processing_repository(request)
    try:
        old_job = repository.get(job_id)
    except (TypeError, ValueError) as exc:
        raise error(404, "image_processing_job_not_found", "图片处理任务不存在") from exc
    if old_job is None:
        raise error(404, "image_processing_job_not_found", "图片处理任务不存在")
    try:
        # retry 请求中的省略字段继承旧 revision 的冻结选项；显式字段仍经过严格规范化。
        options = normalize_processing_options(
            request,
            reverse_image_policy=(payload.reverse_image_policy if payload and payload.reverse_image_policy is not None else old_job.reverse_image_policy),
            auto_name=(payload.auto_name if payload and payload.auto_name is not None else old_job.auto_name),
        )
    except ImageProcessingError as exc:
        status = 404 if exc.code == "job_not_found" else 503 if exc.code == "reverse_image_unavailable" else 422 if exc.code in {"invalid_auto_name", "invalid_reverse_image_policy"} else 409
        raise error(status, exc.code, "图片处理任务当前不可重试") from exc
    try:
        job = repository.retry(job_id, policy=options.reverse_image_policy, auto_name=options.auto_name, config=processing_config(request))
    except (TypeError, ValueError) as exc:
        raise error(404, "image_processing_job_not_found", "图片处理任务不存在") from exc
    except ImageProcessingError as exc:
        status = 404 if exc.code == "job_not_found" else 503 if exc.code == "reverse_image_unavailable" else 422 if exc.code in {"invalid_auto_name", "invalid_reverse_image_policy"} else 409
        raise error(status, exc.code, "图片处理任务当前不可重试") from exc
    worker = processing_worker(request)
    if worker is not None:
        worker.schedule(job.id)
    snapshot = repository.snapshot(job.id)
    if snapshot is None:
        raise error(503, "image_processing_job_unavailable", "图片处理任务当前不可用")
    return snapshot.as_dict()


async def submit_image_stage(
    request: Request,
    payload: ImageStageSubmissionRequest,
    *,
    service: Service,
    error: ErrorFactory,
    processing_worker: ProcessingWorker,
    normalize_processing_options: NormalizeOptions,
    processing_config: ProcessingConfig,
    task_summary: TaskSummary,
    operation_error: OperationError | None = None,
) -> dict[str, object]:
    """提交一个无父 Job 的视觉、Agent、自动重命名或文本 embedding 阶段。

    目标 Meme 和图片均从当前 scope metadata service 派生；返回公开 Task 摘要，不暴露
    payload、路径或 Worker 内部对象。
    """
    try:
        canonical = ImageProcessingWorker._canonical_stage(payload.stage)
    except ImageProcessingError as exc:
        raise error(422, exc.code, "图片阶段无效") from exc
    try:
        record, _image = service(request, "metadata").image_for_meme(payload.meme_id)
    except MetadataError as exc:
        status = 404 if exc.code in {"metadata_missing", "image_unreadable"} else 409
        code = "meme_not_found" if status == 404 else exc.code
        raise error(status, code, "图片不存在或内容已变化") from exc
    try:
        options = normalize_processing_options(request, reverse_image_policy=payload.reverse_image_policy)
    except ImageProcessingError as exc:
        status = 503 if exc.code == "reverse_image_unavailable" else 400
        raise error(status, exc.code, "图片处理选项无效或服务不可用") from exc
    worker = processing_worker(request)
    if worker is None:
        raise error(503, "image_processing_unavailable", "图片处理服务当前不可用")
    try:
        task = worker.submit_stage(
            record.id,
            canonical,
            config=processing_config(request),
            reverse_image_policy=options.reverse_image_policy,
            schedule=True,
        )
    except ImageProcessingError as exc:
        raise _stage_error(exc, error=error, operation_error=operation_error) from exc
    if task is None:
        raise error(503, "image_processing_unavailable", "图片处理任务当前不可用")
    result = task_summary(request, task)
    result.update(
        {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "submission_mode": "standalone",
            "image_stage": task.image_stage or canonical,
            "processing_job_id": None,
        }
    )
    return result


async def submit_image_stage_batch(
    request: Request,
    payload: ImageStageBatchRequest,
    *,
    service: Service,
    error: ErrorFactory,
    processing_worker: ProcessingWorker,
    normalize_processing_options: NormalizeOptions,
    processing_config: ProcessingConfig,
    task_summary: TaskSummary,
) -> dict[str, object]:
    """为选中图片提交一个或多个核心阶段，并隔离逐项失败。

    输入只允许三种核心阶段、当前 scope 的 Meme 标识和统一处理选项；输出保留每项
    成功/失败摘要及实际 task 数量，调用场景是图片工作区批量阶段恢复。
    """
    allowed_stages = {"visual", "agent", "text_embedding"}
    if len(set(payload.stages)) != len(payload.stages) or any(stage not in allowed_stages for stage in payload.stages):
        raise error(422, "invalid_image_stage", "批量阶段只能选择视觉向量、图片语境或文本索引，且不能重复")
    if not payload.items:
        return {"target_count": 0, "submitted_count": 0, "failed_count": 0, "results": []}
    try:
        options = normalize_processing_options(
            request,
            reverse_image_policy=payload.reverse_image_policy,
            auto_name=payload.auto_name,
        )
    except ImageProcessingError as exc:
        status = 503 if exc.code == "reverse_image_unavailable" else 400
        raise error(status, exc.code, "图片处理选项无效或服务不可用") from exc
    worker = processing_worker(request)
    if worker is None:
        raise error(503, "image_processing_unavailable", "图片处理服务当前不可用")
    results: list[dict[str, object]] = []
    for item in payload.items:
        for stage in payload.stages:
            try:
                # 复用单阶段入口的目标和 scope 校验；批量选项在循环前统一规范化。
                record, _image = service(request, "metadata").image_for_meme(item.meme_id)
                task = worker.submit_stage(
                    record.id,
                    stage,
                    config=processing_config(request),
                    reverse_image_policy=options.reverse_image_policy,
                    auto_name=getattr(options, "auto_name", payload.auto_name),
                    schedule=True,
                )
                if task is None:
                    raise ImageProcessingError("image_processing_unavailable")
                result = task_summary(request, task)
                result.update({"meme_id": item.meme_id, "stage": stage})
                results.append(result)
            except MetadataError as exc:
                code = "meme_not_found" if exc.code in {"metadata_missing", "image_unreadable"} else exc.code
                results.append({"meme_id": item.meme_id, "stage": stage, "error": code})
            except ImageProcessingError as exc:
                results.append({"meme_id": item.meme_id, "stage": stage, "error": exc.code})
            except (OSError, RuntimeError) as exc:
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or code not in {
                    "image_processing_unavailable",
                    "operation_forbidden",
                    "operation_limit_exceeded",
                    "operation_policy_unavailable",
                    "stage_submit_failed",
                    "target_changed",
                }:
                    code = "stage_submit_failed"
                results.append({"meme_id": item.meme_id, "stage": stage, "error": code})
    submitted = sum(1 for result in results if result.get("task_id"))
    return {
        "target_count": len(results),
        "submitted_count": submitted,
        "failed_count": len(results) - submitted,
        "results": results,
    }


__all__ = [
    "ImageStageBatchItem",
    "ImageStageBatchRequest",
    "ImageStageSubmissionRequest",
    "ProcessingRetryRequest",
    "get_image_processing_job",
    "list_image_processing_jobs",
    "retry_image_processing_job",
    "submit_image_stage",
    "submit_image_stage_batch",
]
