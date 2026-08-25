"""公共核心图片语境、视觉向量与 metadata repair HTTP 边界。

本模块只编排图片目标解析、处理 Job 提交和公开响应；scope-bound service、环境资源、
任务 service、错误工厂和稳定 enqueue 错误映射由入口通过 callback 注入，不反向依赖
``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.image_processing import ImageProcessingError
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError


class _StrictRequestModel(BaseModel):
    """图片语境 JSON 请求基类，拒绝客户端提交未定义字段。"""

    model_config = ConfigDict(extra="forbid")


class ContextRequest(_StrictRequestModel):
    """按稳定 meme_id 创建图片语境或视觉任务的请求。"""

    meme_id: str | None = None
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")


class ContextBatchRequest(_StrictRequestModel):
    """批量补齐既有图片语境或视觉向量的请求。"""

    items: list[ContextRequest] = Field(default_factory=list, max_length=500)
    include_unready: bool = True
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")


Service = Callable[[Request, str], Any]
Environment = Callable[[Request], AbstractContextManager[Any]]
ErrorFactory = Callable[[int, str, str], HTTPException]
SubmitProcessingJob = Callable[..., Any]
EnqueueError = Callable[[Exception], str]
OperationError = Callable[[OperationPolicyError], HTTPException]


def _target_from_scope(
    request: Request,
    meme_id: str | None,
    *,
    service: Service,
    environment: Environment,
    error: ErrorFactory,
) -> tuple[Any, Any]:
    """从当前 scope 派生 Meme 与受控图片路径。

    输入是客户端提供的稳定 meme_id；输出是 scope-bound Meme 记录和图片路径。sidecar
    指纹暂时不一致时保留旧的数据库 fallback，真正的 target fencing 仍由处理控制面完成。
    """
    if not meme_id:
        raise error(400, "meme_id_required", "必须提供 meme_id")
    try:
        return service(request, "metadata").image_for_meme(meme_id)
    except MetadataError as exc:
        if exc.code == "metadata_image_mismatch":
            raise error(409, "target_changed", "图片内容已变化") from exc
        # 只要数据库 Meme 仍存在，排队请求必须可创建；Worker 会在 claim 后收束目标变化。
        try:
            with environment(request) as resources:
                record = resources.memes.get(meme_id)
        except Exception:  # noqa: BLE001 - fallback 失败时按不存在处理，不泄露底层诊断
            record = None
        if record is None:
            raise error(404, "meme_not_found", "图片不存在") from exc
        image = service(request, "metadata").blob_store.resolve(record.storage_key)
        return record, image


def _job_result(
    snapshot: Any,
    *,
    image_stage: str | None = None,
    include_job_status: bool = False,
    include_task_type: bool = False,
) -> dict[str, object]:
    """按具体旧入口的字段集合投影图片处理 snapshot。"""
    stage = next((item for item in snapshot.stages if item.get("stage") == image_stage), None) if image_stage else next((item for item in snapshot.stages if item.get("stage") == "agent"), None)
    task_id = stage.get("task_id") if stage and stage.get("task_id") else snapshot.job_id
    result: dict[str, object] = {
        "processing_job_id": snapshot.job_id,
        "submission_mode": "pipeline",
        "image_stage": image_stage,
        # 叶子 Task 可能在前置阶段完成后才创建，旧客户端仍使用父 Job 轮询。
        "task_id": task_id,
        "status": snapshot.status,
    }
    if include_job_status:
        result["job_status"] = snapshot.status
    if include_task_type:
        result["task_type"] = "visual_embedding_generation" if image_stage == "visual" else "meme_context_generation"
    return result


async def generate_context(
    request: Request,
    payload: ContextRequest,
    *,
    service: Service,
    environment: Environment,
    submit_processing_job: SubmitProcessingJob,
    error: ErrorFactory,
    operation_error: OperationError | None = None,
) -> dict[str, object]:
    """为单张图片创建或复用统一图片处理 Job。

    输入是当前请求 scope 内的 meme_id 和联网策略；输出是父 Job/Agent 阶段的安全摘要。
    """
    record, image = _target_from_scope(request, payload.meme_id, service=service, environment=environment, error=error)
    try:
        snapshot = submit_processing_job(request, record, image, reverse_image_policy=payload.reverse_image_policy)
    except ImageProcessingError as exc:
        if exc.code in {"operation_forbidden", "operation_limit_exceeded", "operation_policy_unavailable"} and operation_error is not None:
            raise operation_error(OperationPolicyError(exc.code, retry_at=exc.retry_at)) from exc
        status = 404 if exc.code == "job_not_found" else 503 if exc.code in {"image_processing_unavailable", "reverse_image_unavailable"} else 409
        raise error(status, exc.code, "图片处理任务当前不可用") from exc
    return _job_result(snapshot, include_job_status=True, include_task_type=True)


async def generate_context_batch(
    request: Request,
    payload: ContextBatchRequest,
    *,
    service: Service,
    submit_processing_job: SubmitProcessingJob,
    error: ErrorFactory,
    enqueue_error: EnqueueError,
) -> dict[str, object]:
    """批量提交图片语境 Job，并隔离逐项跳过和失败。

    输入是最多 500 个当前 scope meme_id；输出保留稳定 batch_id 与逐项结果，单项异常
    不得阻止后续图片处理。
    """
    batch_id = uuid4().hex
    results: list[dict[str, object]] = []
    for item in payload.items:
        meme_id = item.meme_id
        try:
            if not meme_id:
                raise MetadataError("meme_id_required")
            # 批量入口保持原有逐项语义：不做单图 fallback，底层 metadata code 直接
            # 进入当前项结果，避免一项 scope/目标错误改变其它项的处理顺序。
            record, image = service(request, "metadata").image_for_meme(meme_id)
            state = service(request, "metadata").status(image)["status"]
            # include_unready=False 时保持原语义：已就绪记录只返回 skip，不重新创建 Job。
            if not payload.include_unready and state not in {"pending", "partial", "repair_required"}:
                results.append({"meme_id": meme_id, "skipped": "already_ready"})
                continue
            if state == "repair_required":
                service(request, "metadata").create_pending(image)
            snapshot = submit_processing_job(
                request,
                record,
                image,
                reverse_image_policy=payload.reverse_image_policy,
                explicit_retry=payload.include_unready,
                schedule=True,
            )
            result = _job_result(snapshot)
            result["meme_id"] = meme_id
            results.append(result)
        except (HTTPException, MetadataError, OSError, RuntimeError) as exc:
            error_code = enqueue_error(exc) if isinstance(exc, RuntimeError) else getattr(exc, "code", "context_enqueue_failed")
            results.append({"meme_id": meme_id, "error": error_code})
    return {"batch_id": batch_id, "results": results}


async def generate_visual_embedding(
    request: Request,
    payload: ContextRequest,
    *,
    service: Service,
    submit_processing_job: SubmitProcessingJob,
    error: ErrorFactory,
    enqueue_error: EnqueueError,
) -> dict[str, object]:
    """为既有图片创建或复用统一图片处理 Job 的视觉阶段。

    视觉任务固定使用 forbid 联网策略；处理 Job facade 负责当前 scope 的幂等和 fencing。
    """
    if not payload.meme_id:
        raise error(400, "meme_id_required", "必须提供 meme_id")
    try:
        record, image = service(request, "metadata").image_for_meme(payload.meme_id)
    except MetadataError as exc:
        code = "meme_not_found" if exc.code in {"metadata_missing", "image_unreadable"} else exc.code
        raise error(404 if code == "meme_not_found" else 409, code, "图片不存在或内容已变化") from exc
    try:
        snapshot = submit_processing_job(request, record, image, reverse_image_policy="forbid")
    except (ImageProcessingError, MetadataError, RuntimeError) as exc:
        code = getattr(exc, "code", None) or enqueue_error(exc) if isinstance(exc, RuntimeError) else getattr(exc, "code", "visual_enqueue_failed")
        raise error(409, code, "视觉任务提交失败") from exc
    return _job_result(snapshot, image_stage="visual", include_task_type=True)


async def generate_visual_embedding_batch(
    request: Request,
    payload: ContextBatchRequest,
    *,
    service: Service,
    submit_processing_job: SubmitProcessingJob,
    enqueue_error: EnqueueError,
) -> dict[str, object]:
    """批量提交视觉阶段 Job，并保留逐项错误。

    输入是当前 scope 的图片列表；输出按提交顺序返回 batch_id 和逐项 task 摘要。
    """
    batch_id = uuid4().hex
    results: list[dict[str, object]] = []
    for item in payload.items:
        if not item.meme_id:
            results.append({"meme_id": None, "error": "meme_id_required"})
            continue
        try:
            record, image = service(request, "metadata").image_for_meme(item.meme_id)
            snapshot = submit_processing_job(request, record, image, reverse_image_policy=payload.reverse_image_policy, explicit_retry=True)
            result = _job_result(snapshot, image_stage="visual")
            result["meme_id"] = item.meme_id
            results.append(result)
        except (MetadataError, RuntimeError) as exc:
            results.append({"meme_id": item.meme_id, "error": enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"})
    return {"batch_id": batch_id, "results": results}


async def repair_metadata(request: Request, *, task_service: Service) -> dict[str, object]:
    """提交当前 scope 的幂等 metadata repair 任务。

    输入不接受客户端 body；输出只包含 task_id、task_type 和 status，调用场景是运维/图片
    工作区触发完整性扫描。
    """
    record = task_service(request).submit("metadata_repair", {})
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


__all__ = [
    "ContextRequest",
    "ContextBatchRequest",
    "generate_context",
    "generate_context_batch",
    "generate_visual_embedding",
    "generate_visual_embedding_batch",
    "repair_metadata",
]
