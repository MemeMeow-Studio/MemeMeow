"""公共核心任务控制 HTTP 边界。

本模块负责任务列表、详情、取消、重试和公开摘要投影。scope-bound service、图片处理
repository、错误构造与 Agent 取消能力由入口通过 callback 注入，避免反向依赖 ``api.py``。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Query, Request

from backend.agent_resume import within_total_timeout
from backend.database import DatabaseError
from backend.metadata import MetadataError
from backend.opencode_activity import AgentActivity
from backend.public_dto import (
    PUBLIC_STAGE_STATUSES,
    normalize_public_filename,
    normalize_public_identifier,
    sanitize_public_timestamp,
    sanitize_task_result,
)
from backend.tasks import TaskRecord


Service = Callable[[Request, str], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]
ProcessingRepository = Callable[[Request], Any]
CancelAgent = Callable[[Request, str], None]


def activity_payload(value: object) -> dict[str, object] | None:
    """将 reader 领域值收敛为完整的三个公开活跃度字段。

    输入可以是 ``AgentActivity`` 或 reader 返回的 mapping；字段不完整或类型不安全时
    返回 ``None``。调用场景是任务摘要投影，输出只包含可公开的活跃度字段。
    """
    if isinstance(value, AgentActivity):
        value = value.as_dict()
    if not isinstance(value, Mapping):
        return None
    completed = value.get("agent_completed_turns", value.get("completed_turns"))
    running = value.get("agent_turn_running", value.get("turn_running"))
    last_activity = value.get("agent_last_activity_at", value.get("last_activity_at"))
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        return None
    last_activity = sanitize_public_timestamp(last_activity)
    if not isinstance(running, bool) or last_activity is None:
        return None
    return {
        "agent_completed_turns": completed,
        "agent_turn_running": running,
        "agent_last_activity_at": last_activity,
    }


def read_agent_activity(request: Request, records: list[TaskRecord]) -> dict[str, object]:
    """为当前任务页执行一次有界活跃度批量读取，失败时返回空映射。

    输入是当前 scope 已查询的任务记录；输出按 task id 索引的原始领域值。SQLite 观测
    只提供诊断信息，读取失败不得改变任务 API 的主响应语义。
    """
    task_ids = [record.task_id for record in records if record.task_type == "meme_context_generation"]
    reader = getattr(request.app.state, "agent_activity", None)
    if not task_ids or reader is None:
        return {}
    read_many = getattr(reader, "read_many", None)
    if not callable(read_many):
        read_many = getattr(reader, "read", None)
    if not callable(read_many):
        return {}
    try:
        values = read_many(task_ids)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(values, Mapping):
        return {}
    return {str(task_id): value for task_id, value in values.items() if isinstance(task_id, str)}


def task_summary(
    request: Request,
    record: TaskRecord,
    activities: Mapping[str, object] | None = None,
    *,
    service: Service,
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """将任务转换为安全摘要，并按需装配完整活跃度字段。

    关键输入是当前 scope 的 ``TaskRecord`` 和可选活跃度映射；输出是去除 payload、scope
    身份和物理路径的公开任务 DTO。调用场景是四个任务控制 route 及旧兼容 wrapper。
    """
    if activities is None:
        activities = read_agent_activity(request, [record])
    elif not isinstance(activities, Mapping):
        activities = {}
    data = record.as_dict(include_payload=False)
    payload = record.payload if isinstance(record.payload, Mapping) else {}
    task_type = record.task_type if isinstance(record.task_type, str) else ""
    public_submission_mode = data.get("submission_mode")
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and task_type == "meme_context_generation" and data.get("resume_available"):
        resume_enabled = bool(getattr(settings, "agent_resume_enabled", False))
        if not resume_enabled:
            data["resume_available"] = False
            data["resume_reason"] = "resume_disabled"
        elif isinstance(data.get("resume_attempts"), int) and data["resume_attempts"] >= int(getattr(settings, "agent_resume_max_attempts", 2)):
            data["resume_available"] = False
            data["resume_reason"] = "resume_budget_exhausted"
        elif isinstance(record.resume_started_at, str):
            try:
                resume_started_at = datetime.fromisoformat(record.resume_started_at.replace("Z", "+00:00"))
            except ValueError:
                resume_started_at = None
            if resume_started_at is not None and not within_total_timeout(
                resume_started_at,
                timeout_seconds=int(getattr(settings, "agent_resume_timeout_seconds", 900)),
            ):
                data["resume_available"] = False
                data["resume_reason"] = "resume_budget_exhausted"
    if task_type in {"visual_embedding_generation", "meme_context_generation", "image_auto_rename", "text_embedding_generation"}:
        # NULL 来源只代表旧历史无法可靠归类，不能被前端解释为 standalone。
        data["historical_unclassified"] = public_submission_mode is None
        data["read_only"] = public_submission_mode is None
        data["retry_allowed"] = False
        data["image_stage"] = data.get("image_stage") or {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
            "text_embedding_generation": "text_embedding",
        }.get(task_type)
        if task_type == "image_auto_rename":
            # pipeline 自动命名只有 warning 才能从专用阶段入口恢复；不可降级
            # failure/blocked/unknown_execution 必须保持停止状态。standalone
            # 终态失败则允许按当前 Meme 输入重新提交独立 Task。
            recoverable_errors = {
                "auto_rename_title_missing",
                "auto_rename_invalid_filename",
                "auto_rename_target_exists",
                "auto_rename_target_changed",
            }
            record_error = record.error if isinstance(record.error, Mapping) else {}
            recoverable = public_submission_mode == "standalone" and record.status == "failed" and record_error.get("error") in recoverable_errors
            processing_job_id = data.get("processing_job_id")
            if public_submission_mode == "pipeline" and isinstance(processing_job_id, str) and processing_job_id:
                try:
                    processing = processing_repository(request).snapshot(processing_job_id)
                    stage = next((item for item in processing.stages if isinstance(item, Mapping) and item.get("stage") == "auto_rename"), None) if processing else None
                    recoverable = bool(stage and stage.get("status") == "warning")
                    stage_status = stage.get("status") if stage and isinstance(stage.get("status"), str) and stage.get("status") in PUBLIC_STAGE_STATUSES else None
                    data["image_stage_status"] = stage_status
                except (DatabaseError, TypeError, ValueError):
                    recoverable = False
            data["image_stage_recoverable"] = recoverable
    if task_type == "meme_context_generation":
        # 只暴露可观察策略；完整 payload 仍留在后端数据库和 Worker 边界内。
        policy = payload.get("reverse_image_policy")
        data["reverse_image_policy"] = policy if isinstance(policy, str) and policy in {"forbid", "auto"} else "forbid"
        activity = activity_payload(activities.get(record.task_id)) if isinstance(record.task_id, str) else None
        if activity is not None:
            data.update(activity)
    elif task_type == "visual_embedding_generation":
        visual_result = sanitize_task_result(
            "visual_embedding_generation",
            {
                "visual_model": payload.get("visual_model"),
                "dimensions": payload.get("visual_dimensions"),
                "preprocess_version": payload.get("preprocess_version"),
            },
        ) or {}
        data["visual"] = {
            "model": visual_result.get("visual_model"),
            "dimensions": visual_result.get("dimensions"),
            "preprocess_version": visual_result.get("preprocess_version"),
        }
    meme_id = normalize_public_identifier(payload.get("meme_id"))
    if meme_id:
        try:
            _meme_record, image = service(request, "metadata").image_for_meme(meme_id)
            image_data: dict[str, object] = {"meme_id": meme_id, "media_url": f"/media/{meme_id}"}
            filename = normalize_public_filename(getattr(image, "name", None))
            if filename:
                image_data["filename"] = filename
            data["image"] = image_data
        except MetadataError:
            data["image"] = {"meme_id": meme_id}
    return data


async def list_tasks(
    request: Request,
    *,
    status: list[str] = Query(default=[]),
    task_type: list[str] = Query(default=[]),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    service: Service,
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """按筛选条件分页列出任务安全摘要。

    输出包含 ``items`` 与 ``next_cursor``；任务查询始终通过注入的当前 scope service 完成。
    """
    records, next_cursor = service(request, "tasks").list(statuses=set(status) or None, task_types=set(task_type) or None, cursor=cursor, limit=limit)
    activities = read_agent_activity(request, records)
    return {"items": [task_summary(request, record, activities, service=service, processing_repository=processing_repository) for record in records], "next_cursor": next_cursor}


async def get_task(
    request: Request,
    task_id: str,
    *,
    service: Service,
    error: ErrorFactory,
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """查询持久任务详情，并保留图片处理父 Job 的旧轮询回退。

    找不到普通任务时只查询当前 scope 的图片处理 repository；两者均缺失时返回稳定
    ``task_not_found`` 错误，不泄露其它 scope 的任务事实。
    """
    record = service(request, "tasks").get(task_id)
    if record is None:
        # 图片处理 job 与叶子 Task 使用不同控制面，但旧前端只知道统一的
        # ``/tasks/{id}`` 轮询入口；回退查询仍严格绑定当前请求 scope。
        try:
            snapshot = processing_repository(request).snapshot(task_id)
        except (TypeError, ValueError):
            snapshot = None
        if snapshot is not None:
            # 上传/合集旧客户端把父 Job 当作视觉任务轮询；新客户端使用
            # /images/processing/{job_id} 获取完整四阶段 DTO。这里只保留
            # 旧轮询所需的任务类型兼容值，不改变父 Job 的真实状态。
            data = snapshot.as_dict()
            data["task_type"] = "visual_embedding_generation"
            data["image_stage"] = "visual"
            return data
        raise error(404, "task_not_found", "任务不存在")
    return task_summary(request, record, service=service, processing_repository=processing_repository)


async def cancel_task(
    request: Request,
    task_id: str,
    *,
    service: Service,
    error: ErrorFactory,
    processing_repository: ProcessingRepository,
    cancel_agent: CancelAgent | None = None,
) -> dict[str, object]:
    """取消单个未完成任务，不停止共享 Agent 容器或其他 session。

    已完成任务直接返回摘要；任务服务负责真正的状态转换，Agent 取消只作为可选适配
    能力按当前任务类型调用。
    """
    tasks = service(request, "tasks")
    record = tasks.get(task_id)
    if record is None:
        raise error(404, "task_not_found", "任务不存在")
    if record.status in {"succeeded", "failed"}:
        return task_summary(request, record, service=service, processing_repository=processing_repository)
    if not tasks.cancel(task_id):
        record = tasks.get(task_id)
        if record is None:
            raise error(404, "task_not_found", "任务不存在")
    if record.task_type == "meme_context_generation" and cancel_agent is not None:
        cancel_agent(request, task_id)
    current = tasks.get(task_id)
    return task_summary(request, current or record, service=service, processing_repository=processing_repository)


async def retry_task(
    request: Request,
    task_id: str,
    *,
    service: Service,
    error: ErrorFactory,
    processing_repository: ProcessingRepository,
) -> dict[str, object]:
    """只重试当前失败阶段；视觉/Agent/文本任务不会隐式级联。

    task service 的稳定 RuntimeError code 映射为既有 HTTP 错误；未知诊断文本不直接
    暴露给客户端。
    """
    try:
        record = service(request, "tasks").retry(task_id)
    except RuntimeError as exc:
        code = str(exc).split(":", 1)[0]
        if code == "task_not_found":
            raise error(404, code, "任务不存在") from exc
        if code == "task_not_failed":
            raise error(409, code, "只有失败任务可以重试") from exc
        if code == "image_stage_retry_forbidden":
            raise error(409, code, "图片阶段必须通过完整 Job 或专用阶段入口重试") from exc
        if code == "agent_backpressure":
            raise error(429, code, "Agent 等待队列已满，请稍后重试") from exc
        raise error(409, code, "任务重试失败") from exc
    return task_summary(request, record, service=service, processing_repository=processing_repository)


__all__ = [
    "activity_payload",
    "cancel_task",
    "get_task",
    "list_tasks",
    "read_agent_activity",
    "retry_task",
    "task_summary",
]
