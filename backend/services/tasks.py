from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

import threading
from sqlalchemy import select

from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    ImageProcessingAttempt,
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    ScopeContext,
    SearchGeneration,
    StorageOperation,
    Task,
    TaskBatch,
    GLOBAL_LANE_RESOURCE_KEY,
    TaskLaneSlot,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.persistence.models import utcnow
from backend.agent_resume import (
    append_error_history,
    append_task_error_history,
    agent_failure_requires_unknown,
    bounded_backoff,
    classify_resume_error,
    normalize_config_hash,
    normalize_identifier,
    sanitize_error,
    sanitize_error_history,
    within_total_timeout,
)
from backend.config import validate_agent_concurrency
from executor.agent_limits import validate_agent_concurrency_at_most
from backend.operation_policy import GrantAssociation, GrantAssociationStore, OperationPolicyError, OperationPolicyGateway, Operations, require_allowed
from backend.opencode_workspace import SELECTOR_RE
from backend.public_dto import sanitize_task_result
from backend.tasks import (
    IMAGE_PROCESSING_TASK_TYPES,
    TaskRecord,
    TERMINAL,
    STABLE_TASK_ERRORS,
)
from backend.scope import validate_scope_services
from backend.services.worker_manager import PostgresTaskWorkerManager
from backend.persistence.repositories.tasks import validate_lane_resource_key, validate_lane_resource_concurrency
from backend.visual_snapshot import VisualMatchSnapshotError, validate_visual_match_snapshot, visual_match_snapshot_summary

# 任务服务沿用旧 facade logger，确保失败/unknown 运营日志不改变来源。
logger = logging.getLogger("backend.pg_services")
# 任务 payload 只承载业务输入；范围事实始终来自持久 Task.scope_id。
UNTRUSTED_SCOPE_FIELDS = frozenset({"scope_id", "scope-id", "user_id", "user-id"})


def _is_explicit_image_task(record: object) -> bool:
    """判断图片任务是否已经绑定新控制面的显式来源。"""
    return (
        getattr(record, "task_type", None) in IMAGE_PROCESSING_TASK_TYPES
        and getattr(record, "submission_mode", None) in {"pipeline", "standalone"}
    )


def _iso(value: datetime | str | None) -> str:
    """将数据库时间转换为旧领域模型接受的 UTC ISO 字符串。"""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat() if isinstance(value, datetime) else str(value)

class PostgresTaskService:
    """使用指定 scope 记录、去重、租约和 claim fencing 的任务执行器。

    直接构造时的 local 默认值仅用于开源兼容夹具；生产 Worker 由 scope factory 装配。
    ``scope`` 只用于选择任务表中的候选行；真正执行时仍从刚认领的 Task 行
    恢复并校验 scope，避免普通 payload 或 Worker 的历史默认值成为归属事实。
    """

    def __init__(self, resources: DatabaseResources, *, scope_id: str | ScopeContext = "local", agent_concurrency: int = 1, scope_concurrency: int | None = None, resource_concurrency: Mapping[str, int] | None = None, agent_backpressure: int | None = None, settings_version: str | None = None, lease_seconds: int = 120, max_attempts: int = 3, executor: ThreadPoolExecutor | None = None, worker_manager: PostgresTaskWorkerManager | None = None, finalize_image_tasks: bool = True, operation_policy: OperationPolicyGateway | None = None, grant_store: GrantAssociationStore | None = None, visual_snapshot_preparer: Callable[..., Mapping[str, Any]] | None = None, visual_candidate_preparer: Callable[..., Any] | None = None, resume_enabled: bool = False, resume_max_attempts: int = 2, resume_backoff_seconds: int = 2, resume_max_backoff_seconds: int = 60, resume_timeout_seconds: int = 900):
        """绑定任务资源、scope、并发/租约配置和可选的进程级 Worker manager。

        ``agent_backpressure`` 仅为旧调用方保留，不参与 Agent 运行槽位或队列判定。
        """
        del agent_backpressure
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.agent_concurrency = validate_agent_concurrency(agent_concurrency)
        self.agent_scope_concurrency = validate_agent_concurrency_at_most(
            scope_concurrency if scope_concurrency is not None else 1,
            self.agent_concurrency,
            error_code="agent_scope_concurrency_exceeds_global",
        )
        configured_resources = resource_concurrency if resource_concurrency is not None else getattr(worker_manager, "resource_concurrency", None)
        self.resource_concurrency = validate_lane_resource_concurrency(configured_resources, self.agent_concurrency)
        self.settings_version = settings_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.resume_enabled = bool(resume_enabled)
        self.resume_max_attempts = max(0, min(int(resume_max_attempts), 10))
        self.resume_backoff_seconds = max(0, min(int(resume_backoff_seconds), 300))
        self.resume_max_backoff_seconds = max(0, min(int(resume_max_backoff_seconds), 3600))
        self.resume_timeout_seconds = max(1, min(int(resume_timeout_seconds), 86400))
        # 图片 Worker 执行叶子任务时关闭旧批次 finalizer，避免再次隐式创建
        # cache_generation；普通兼容 facade 仍保留既有显式批次能力。
        self._finalize_image_tasks = bool(finalize_image_tasks)
        self._handlers: dict[str, Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]] = {}
        self._batch_finalizer: Callable[[str], Any] | None = None
        self._worker_manager = worker_manager
        # 只有图片专用 facade 注入该二元组时才启用 Agent grant 复核；普通
        # 兼容任务服务仍可执行历史任务，但不会替客户端伪造计量事实。
        self._operation_policy = operation_policy
        self._grant_store = grant_store
        self._visual_snapshot_preparer = visual_snapshot_preparer
        self._visual_candidate_preparer = visual_candidate_preparer
        self._executor = worker_manager.executor if worker_manager is not None else executor or ThreadPoolExecutor(max_workers=max(2, self.agent_concurrency + 1), thread_name_prefix="mememeow-pg-task")
        self._owns_executor = worker_manager is None and executor is None
        self._lock = Lock()
        self._stopped = Event()
        self._scheduled: set[str] = set()
        self.owner = worker_manager.owner if worker_manager is not None else f"worker-{os.getpid()}-{id(self)}"

    def resource_capacity(self, resource_key: str | None) -> int:
        """返回资源 key 的运行容量；缺失映射时继承全局 Agent 容量。"""
        try:
            key = validate_lane_resource_key(resource_key)
        except ValueError as exc:
            raise DatabaseError("agent_resource_key_invalid") from exc
        return self.resource_concurrency.get(key, self.agent_concurrency)

    def register(self, task_type: str, handler: Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]) -> None:
        """注册由数据库 payload 重建的同步处理器。"""
        if self._worker_manager is not None:
            self._worker_manager.register(task_type, handler)
        else:
            self._handlers[task_type] = handler

    def set_batch_finalizer(self, callback: Callable[[str], Any] | None) -> None:
        """注册批次全部终态后的单次收束回调。"""
        self._batch_finalizer = callback

    def seal_batch(self, batch_id: str) -> None:
        """封口批次并在同一数据库事务中持久化唯一缓存任务。"""
        created_task_id: str | None = None
        with self.resources.environment(self.scope.scope_id) as environment:
            environment.tasks.seal_batch(batch_id)
            task = environment.tasks.finalize_batch_with_task(
                batch_id,
                task_type="cache_generation",
                payload={},
                dedupe_key="cache_generation",
                settings_version=self.settings_version,
                max_attempts=self.max_attempts,
            )
            if task is not None:
                created_task_id = task.id
        if created_task_id:
            self._schedule(created_task_id)

    @staticmethod
    def _dedupe(task_type: str, payload: dict[str, Any]) -> str:
        """为普通任务和图片阶段任务生成包含来源模式的稳定活动去重键。"""
        mode = str(payload.get("submission_mode") or ("pipeline" if payload.get("job_id") else "legacy"))
        stage = str(payload.get("stage") or {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
            "text_embedding_generation": "text_embedding",
        }.get(task_type) or "legacy")
        if task_type == "visual_embedding_generation":
            return "visual:{mode}:{stage}:{meme}:{sha}:{model}:{preprocess}:{config}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                model=payload.get("visual_model"),
                preprocess=payload.get("preprocess_version"),
                config=payload.get("processing_config_hash") or "legacy",
            )
        if task_type == "meme_context_generation":
            return "context:{mode}:{stage}:{meme}:{sha}:{config}:{policy}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                config=payload.get("processing_config_hash") or payload.get("skill_hash") or payload.get("model"),
                policy=payload.get("reverse_image_policy") or "forbid",
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "image_auto_rename":
            return "rename:{mode}:{stage}:{meme}:{sha}:{storage}:{title}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                storage=payload.get("expected_storage_key"),
                title=payload.get("title_fingerprint"),
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "text_embedding_generation":
            return "text:{mode}:{stage}:{meme}:{sha}:{metadata}:{model}:{config}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                metadata=payload.get("metadata_hash") or "unknown",
                model=payload.get("embedding_model") or payload.get("model"),
                config=payload.get("processing_config_hash") or "legacy",
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "cache_generation":
            return "cache_generation"
        return f"{task_type}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    def _context_policy_conflict(self, payload: dict[str, Any], dedupe: str) -> None:
        """拒绝同一图片活动任务的策略不一致提交，避免静默复用错误权限。"""
        if payload.get("reverse_image_policy") not in {"forbid", "auto"}:
            return
        requested_mode = payload.get("submission_mode") if payload.get("submission_mode") in {"pipeline", "standalone"} else None
        with self.resources.environment(self.scope.scope_id) as environment:
            existing = environment.uow.session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.task_type == "meme_context_generation",
                    Task.submission_mode == requested_mode,
                    Task.dedupe_key.like(f"context:%:{payload.get('meme_id')}:{payload.get('image_sha256')}:%"),
                    Task.status.in_(('queued', 'running')),
                )
            )
            if existing is not None:
                current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
                current_config = str((existing.payload or {}).get("processing_config_hash") or (existing.payload or {}).get("skill_hash") or (existing.payload or {}).get("model") or "")
                requested_config = str(payload.get("processing_config_hash") or payload.get("skill_hash") or payload.get("model") or "")
                if current != str(payload.get("reverse_image_policy")) or current_config != requested_config:
                    raise RuntimeError("generation_policy_conflict")

    @staticmethod
    def _assert_context_policy(existing: Task, requested: dict[str, Any]) -> None:
        """在任务 repository 复用活动任务后再次核对策略，覆盖预检与插入之间的竞态。"""
        current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
        wanted = str(requested.get("reverse_image_policy") or "forbid")
        current_config = str((existing.payload or {}).get("processing_config_hash") or (existing.payload or {}).get("skill_hash") or (existing.payload or {}).get("model") or "")
        wanted_config = str(requested.get("processing_config_hash") or requested.get("skill_hash") or requested.get("model") or "")
        if current != wanted or current_config != wanted_config:
            raise RuntimeError("generation_policy_conflict")

    def start(self) -> None:
        """启动数据库任务恢复调度，包括队列和过期租约。"""
        if self._worker_manager is not None:
            self._worker_manager.start()
            return
        owned_types = None if self._finalize_image_tasks else IMAGE_PROCESSING_TASK_TYPES
        with self.resources.environment(self.scope.scope_id) as environment:
            queued = environment.tasks.recover_expired(
                owner=self.owner,
                limit=5000,
                exclude_task_types=IMAGE_PROCESSING_TASK_TYPES if owned_types is not None else None,
                include_task_types=owned_types,
                exclude_image_pipeline=owned_types is None,
            )
            # 普通 facade 需要恢复旧批次 finalizer；图片专用 facade 禁止重新
            # 创建 scope 级 cache_generation。
            pending_batches = environment.tasks.pending_finalizer_batches(limit=5000) if self._finalize_image_tasks else []
            cursor = None
            while True:
                records, cursor = environment.tasks.list(statuses={"queued"}, cursor=cursor, limit=100)
                queued.extend(
                    record.id
                    for record in records
                    if (
                        record.task_type in owned_types
                        if owned_types is not None
                        else not _is_explicit_image_task(record)
                    )
                )
                if cursor is None:
                    break
            for batch_id in pending_batches:
                task = environment.tasks.finalize_batch_with_task(
                    batch_id,
                    task_type="cache_generation",
                    payload={},
                    dedupe_key="cache_generation",
                    settings_version=self.settings_version,
                    max_attempts=self.max_attempts,
                )
                if task is not None:
                    queued.append(task.id)
        for task_id in dict.fromkeys(queued):
            self._schedule(task_id)

    def _record_to_dataclass(self, record: Any, *, slot_id: int | None = None) -> TaskRecord:
        """将 ORM 任务转换为 API/旧领域共用的安全快照。"""
        session_id = normalize_identifier(getattr(record, "resume_session_id", None), kind="session")
        executor_attempt_id = normalize_identifier(getattr(record, "executor_attempt_id", None), kind="attempt")
        stored_resume_available = bool(getattr(record, "resume_available", False))
        return TaskRecord(
            task_id=record.id,
            task_type=record.task_type,
            submission_mode=getattr(record, "submission_mode", None),
            image_stage=getattr(record, "image_stage", None),
            processing_job_id=str(getattr(record, "processing_job_id", "")) if getattr(record, "processing_job_id", None) else None,
            lane_resource_key=getattr(record, "lane_resource_key", GLOBAL_LANE_RESOURCE_KEY) or GLOBAL_LANE_RESOURCE_KEY,
            payload=dict(record.payload or {}),
            visual_snapshot_sha256=getattr(record, "visual_snapshot_sha256", None),
            visual_snapshot_protocol_version=getattr(record, "visual_snapshot_protocol_version", None),
            visual_snapshot_matched_at=_iso(getattr(record, "visual_snapshot_matched_at", None)) if getattr(record, "visual_snapshot_matched_at", None) else None,
            visual_snapshot_candidate_count=getattr(record, "visual_snapshot_candidate_count", None),
            status=record.status,
            progress=record.progress,
            message=record.message,
            created_at=_iso(record.created_at),
            updated_at=_iso(record.updated_at),
            completed_at=_iso(record.completed_at) if record.completed_at else None,
            attempts=record.attempt_count,
            error=sanitize_error(record.error) if isinstance(getattr(record, "error", None), dict) else None,
            resume_available=bool(stored_resume_available and session_id and executor_attempt_id),
            resume_reason=("session_not_resumable" if stored_resume_available and not (session_id and executor_attempt_id) else getattr(record, "resume_reason", None)),
            session_id=session_id,
            executor_attempt_id=executor_attempt_id,
            workspace_selector=getattr(record, "workspace_selector", None) if isinstance(getattr(record, "workspace_selector", None), str) else None,
            resume_attempts=int(getattr(record, "resume_attempt_count", 0) or 0),
            resume_started_at=_iso(getattr(record, "resume_started_at", None)) if getattr(record, "resume_started_at", None) else None,
            first_error=sanitize_error(getattr(record, "first_error", None)) if isinstance(getattr(record, "first_error", None), dict) else None,
            error_history=sanitize_error_history(getattr(record, "error_history", None)),
            result=sanitize_task_result(record.task_type, record.result),
            settings_version=record.settings_version,
            agent_concurrency=self.agent_concurrency if record.lane == "agent" else None,
            slot_id=slot_id,
            scope_id=record.scope_id,
        )

    @staticmethod
    def _image_attempt_input_digest(payload: Mapping[str, Any], snapshot: Mapping[str, Any] | None = None) -> str:
        """计算图片 attempt 的稳定输入摘要，并补入 snapshot 已冻结的视觉身份。

        ``_prepare_visual_snapshot`` 可能为旧任务补齐模型、维度和预处理版本；这些
        字段属于同一业务输入，必须让首次 attempt 与后续 resume 使用完全相同的摘要。
        """
        stable_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        if snapshot is not None:
            query = snapshot.get("query")
            if isinstance(query, Mapping):
                for payload_key, query_key in (
                    ("visual_model", "model"),
                    ("visual_dimensions", "dimensions"),
                    ("preprocess_version", "preprocess_version"),
                ):
                    if payload_key not in stable_payload and query_key in query:
                        stable_payload[payload_key] = query[query_key]
        return hashlib.sha256(
            json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _processing_config_hash(payload: Mapping[str, Any], query: Mapping[str, Any] | None = None) -> str:
        """规范化任务处理配置指纹，并为旧任务补出稳定值。

        新任务直接复用控制面写入的 SHA-256；迁移前任务可能只有少量模型配置字段，
        此处按固定白名单和视觉 query 身份计算同一摘要，供 grant、attempt 和 resume
        使用。不会把图片内容、claim 或恢复标识当作配置事实。
        """
        raw = payload.get("processing_config_hash")
        if raw is not None:
            normalized = normalize_config_hash(raw)
            if normalized is None:
                raise RuntimeError("visual_match_snapshot_invalid")
            return normalized
        config_fields = (
            "model",
            "agent_model",
            "skill_hash",
            "settings_version",
            "visual_model",
            "visual_dimensions",
            "preprocess_version",
            "embedding_model",
            "embedding_dimensions",
        )
        config: dict[str, Any] = {
            key: payload[key]
            for key in config_fields
            if key in payload and payload[key] is not None
        }
        if query is not None:
            for payload_key, query_key in (
                ("visual_model", "model"),
                ("visual_dimensions", "dimensions"),
                ("preprocess_version", "preprocess_version"),
            ):
                if payload_key not in config and query_key in query:
                    config[payload_key] = query[query_key]
        try:
            encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RuntimeError("visual_match_snapshot_invalid") from exc
        return hashlib.sha256(encoded).hexdigest()

    def _image_attempt_state(self, claim: Task, payload: dict[str, Any], state: str) -> None:
        """保存图片叶子当前 claim 的 attempt 状态，供重启恢复辨认未知执行。"""
        if claim.task_type not in IMAGE_PROCESSING_TASK_TYPES:
            return
        mode = payload.get("submission_mode")
        if mode == "pipeline" and not isinstance(payload.get("job_id"), str):
            return
        if mode not in {None, "pipeline", "standalone"}:
            return
        target_sha = payload.get("image_sha256")
        if not isinstance(target_sha, str) or len(target_sha) != 64:
            return
        # claim、resume 和 attempt 绑定字段都是运行时事实，不属于同一输入的
        # 业务摘要；排除全部内部字段才能让续跑 attempt 与原 attempt 对齐。
        snapshot_summary: dict[str, object] | None = None
        raw_snapshot = payload.get("_visual_match_snapshot")
        if raw_snapshot is not None:
            try:
                snapshot_summary = visual_match_snapshot_summary(raw_snapshot)
            except VisualMatchSnapshotError:
                # attempt 不能保存未经完整 hash 校验的视觉事实；上层会把任务
                # 收束为稳定的 snapshot 错误，而不是留下可恢复的半成品。
                return
        input_digest = self._image_attempt_input_digest(
            payload,
            raw_snapshot if isinstance(raw_snapshot, Mapping) else None,
        )
        now = utcnow()
        raw_selector = payload.get("_workspace_selector")
        if raw_selector is not None and (
            not isinstance(raw_selector, str)
            or not SELECTOR_RE.fullmatch(raw_selector)
            or (self.scope.scope_id != "local" and raw_selector == "local")
        ):
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            current_task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == claim.id,
                    Task.status == "running",
                    Task.claim_generation == claim.claim_generation,
                    Task.lease_owner == self.owner,
                    Task.lease_expires_at > now,
                )
            )
            if current_task is None:
                return
            row = session.scalar(
                select(ImageProcessingAttempt).where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt == claim.attempt_count,
                ).with_for_update()
            )
            if row is None:
                row = ImageProcessingAttempt(
                    scope_id=self.scope.scope_id,
                    task_id=claim.id,
                    attempt=claim.attempt_count,
                    attempt_id=uuid4().hex,
                    stage=str(payload.get("stage") or claim.task_type),
                    state=state,
                    request_id=str(payload.get("request_id")) if payload.get("request_id") else None,
                    session_id=str(payload.get("_resume_session_id") or payload.get("session_id")) if (payload.get("_resume_session_id") or payload.get("session_id")) else None,
                    executor_attempt_id=normalize_identifier(payload.get("_executor_attempt_id"), kind="attempt"),
                    resume_of_attempt_id=normalize_identifier(payload.get("_resume_of_attempt_id"), kind="attempt"),
                    workspace_selector=str(payload.get("_workspace_selector")) if isinstance(payload.get("_workspace_selector"), str) and SELECTOR_RE.fullmatch(str(payload.get("_workspace_selector"))) else None,
                    processing_config_hash=normalize_config_hash(payload.get("processing_config_hash")),
                    input_digest=input_digest,
                    target_sha256=target_sha.lower(),
                    claim_generation=claim.claim_generation,
                    visual_snapshot_sha256=(str(snapshot_summary["snapshot_sha256"]) if snapshot_summary is not None else payload.get("_visual_snapshot_sha256") if isinstance(payload.get("_visual_snapshot_sha256"), str) else None),
                    visual_snapshot_protocol_version=(int(snapshot_summary["protocol_version"]) if snapshot_summary is not None else payload.get("_visual_snapshot_protocol_version") if isinstance(payload.get("_visual_snapshot_protocol_version"), int) and not isinstance(payload.get("_visual_snapshot_protocol_version"), bool) else None),
                    visual_snapshot_matched_at=(datetime.fromisoformat(str(snapshot_summary["matched_at"]).replace("Z", "+00:00")) if snapshot_summary is not None else payload.get("_visual_snapshot_matched_at") if isinstance(payload.get("_visual_snapshot_matched_at"), datetime) else None),
                    visual_snapshot_candidate_count=(int(snapshot_summary["candidate_count"]) if snapshot_summary is not None else payload.get("_visual_snapshot_candidate_count") if isinstance(payload.get("_visual_snapshot_candidate_count"), int) and not isinstance(payload.get("_visual_snapshot_candidate_count"), bool) else None),
                )
                session.add(row)
            else:
                # 旧 Worker 不能把新 claim 的 attempt 状态覆盖回去。
                if row.claim_generation != claim.claim_generation:
                    return
                if isinstance(raw_selector, str) and row.workspace_selector is not None and row.workspace_selector != raw_selector:
                    return
                row.state = state
                row.updated_at = now
                if normalize_identifier(payload.get("_resume_session_id"), kind="session"):
                    row.session_id = str(payload["_resume_session_id"])
                if normalize_identifier(payload.get("_executor_attempt_id"), kind="attempt"):
                    row.executor_attempt_id = str(payload["_executor_attempt_id"])
                if isinstance(payload.get("_workspace_selector"), str) and SELECTOR_RE.fullmatch(str(payload["_workspace_selector"])):
                    row.workspace_selector = str(payload["_workspace_selector"])
                # 迁移前任务首次写 attempt 时可能尚无配置 hash；snapshot
                # 前置器随后会在同一 claim 补齐该字段，恢复事实必须同步更新。
                if getattr(row, "processing_config_hash", None) is None and payload.get("processing_config_hash") is not None:
                    normalized_config_hash = normalize_config_hash(payload.get("processing_config_hash"))
                    if normalized_config_hash is None:
                        return
                    row.processing_config_hash = normalized_config_hash
                if snapshot_summary is not None:
                    # 首次 ``prepared`` 可能发生在 snapshot 生成之前；snapshot
                    # 身份补齐后必须更新摘要，避免 resume 查询不到原 attempt。
                    row.input_digest = input_digest
                    row.visual_snapshot_sha256 = str(snapshot_summary["snapshot_sha256"])
                    row.visual_snapshot_protocol_version = int(snapshot_summary["protocol_version"])
                    row.visual_snapshot_matched_at = datetime.fromisoformat(str(snapshot_summary["matched_at"]).replace("Z", "+00:00"))
                    row.visual_snapshot_candidate_count = int(snapshot_summary["candidate_count"])
                else:
                    if isinstance(payload.get("_visual_snapshot_sha256"), str):
                        row.visual_snapshot_sha256 = payload["_visual_snapshot_sha256"]
                    if isinstance(payload.get("_visual_snapshot_protocol_version"), int) and not isinstance(payload.get("_visual_snapshot_protocol_version"), bool):
                        row.visual_snapshot_protocol_version = payload["_visual_snapshot_protocol_version"]
                    if isinstance(payload.get("_visual_snapshot_matched_at"), datetime):
                        row.visual_snapshot_matched_at = payload["_visual_snapshot_matched_at"]
                    if isinstance(payload.get("_visual_snapshot_candidate_count"), int) and not isinstance(payload.get("_visual_snapshot_candidate_count"), bool):
                        row.visual_snapshot_candidate_count = payload["_visual_snapshot_candidate_count"]
            session.commit()

    def record_agent_attempt(
        self,
        payload: dict[str, Any],
        *,
        error: dict[str, Any] | None = None,
        session_id: str | None = None,
        executor_attempt_id: str | None = None,
        workspace_selector: str | None = None,
        resume_available: bool = False,
        resume_reason: str | None = None,
    ) -> bool:
        """在当前 claim fencing 下持久化 Agent session、executor attempt 和失败历史。"""
        task_id = payload.get("_claim_task_id")
        generation = payload.get("_claim_generation")
        owner = payload.get("_claim_owner")
        attempt = payload.get("_claim_attempt")
        if not isinstance(task_id, str) or not isinstance(generation, int) or not isinstance(owner, str) or not isinstance(attempt, int):
            return False
        safe_session = normalize_identifier(session_id, kind="session")
        safe_executor_attempt = normalize_identifier(executor_attempt_id, kind="attempt")
        supplied_selector = workspace_selector if workspace_selector is not None else payload.get("_workspace_selector")
        if supplied_selector is not None and (
            not isinstance(supplied_selector, str)
            or not SELECTOR_RE.fullmatch(supplied_selector)
            or (self.scope.scope_id != "local" and supplied_selector == "local")
        ):
            return False
        safe_workspace_selector = supplied_selector if isinstance(supplied_selector, str) else None
        if self.scope.scope_id != "local" and safe_workspace_selector is None:
            # non-local attempt 缺少绑定时不能把失败或成功写成可继续执行的事实。
            return False
        payload_config_hash = normalize_config_hash(payload.get("processing_config_hash"))
        if payload.get("processing_config_hash") is not None and payload_config_hash is None:
            return False
        now = utcnow()
        # 原因码只保存在内部 attempt 记录；公开任务 DTO 默认会丢弃该附加字段。
        safe_error = sanitize_error(error, include_reason_code=True) if error else None
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == task_id,
                    Task.status == "running",
                    Task.claim_generation == generation,
                    Task.lease_owner == owner,
                    Task.lease_expires_at > now,
                )
                .with_for_update()
            )
            if task is None:
                session.commit()
                return False
            row = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == task_id,
                    ImageProcessingAttempt.attempt == attempt,
                )
                .with_for_update()
            )
            if row is None:
                session.commit()
                return False
            if row.claim_generation != generation:
                # attempt 行也必须与当前 claim generation 一致；只校验 Task
                # 行会允许旧 attempt 借用同一 attempt 序号写入新 claim。
                session.commit()
                return False
            if normalize_config_hash(row.processing_config_hash) != payload_config_hash:
                session.commit()
                return False
            if safe_workspace_selector is not None and row.workspace_selector is not None and row.workspace_selector != safe_workspace_selector:
                session.commit()
                return False
            if safe_workspace_selector is not None and task.workspace_selector is not None and task.workspace_selector != safe_workspace_selector:
                session.commit()
                return False
            if safe_session:
                row.session_id = safe_session
                task.resume_session_id = safe_session
            if safe_executor_attempt:
                row.executor_attempt_id = safe_executor_attempt
                task.executor_attempt_id = safe_executor_attempt
            if safe_workspace_selector:
                row.workspace_selector = safe_workspace_selector
                task.workspace_selector = safe_workspace_selector
            if safe_error:
                row.error = safe_error
                row.resume_reason = resume_reason or safe_error.get("error")
            row.resume_available = bool(resume_available and safe_session and safe_executor_attempt)
            row.state = "failed" if safe_error else "completed"
            row.updated_at = now
            if safe_error:
                task.first_error = sanitize_error(task.first_error) if isinstance(task.first_error, dict) else safe_error
                task.error_history = append_error_history(
                    task.error_history,
                    safe_error,
                    attempt=attempt,
                    executor_attempt_id=safe_executor_attempt,
                    session_id=safe_session,
                    occurred_at=now.isoformat(),
                )
                task.resume_available = bool(resume_available and safe_session and safe_executor_attempt)
                task.resume_reason = resume_reason or safe_error.get("error")
                if task.resume_available and task.resume_started_at is None:
                    task.resume_started_at = now
            else:
                task.resume_available = False
                task.resume_reason = None
            session.commit()
            return True

    def _resume_candidate(self, claim: Task, payload: dict[str, Any]) -> dict[str, str] | None:
        """读取并校验同一任务最近的可续跑 attempt，拒绝猜测 session。"""
        if not self.resume_enabled or claim.task_type != "meme_context_generation":
            return None
        if claim.resume_attempt_count >= self.resume_max_attempts:
            return None
        if not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
            return None
        target_sha = payload.get("image_sha256")
        if not isinstance(target_sha, str) or len(target_sha) != 64:
            return None
        try:
            config_hash = self._processing_config_hash(payload)
        except RuntimeError:
            raise
        snapshot_for_digest: Mapping[str, Any] | None = None
        raw_snapshot = getattr(claim, "visual_match_snapshot", None)
        if raw_snapshot is not None:
            try:
                snapshot_for_digest = validate_visual_match_snapshot(raw_snapshot)
            except VisualMatchSnapshotError as exc:
                raise RuntimeError("visual_match_snapshot_invalid") from exc
        input_digest = self._image_attempt_input_digest(payload, snapshot_for_digest)
        # 修复前的 Worker 曾在 snapshot 身份补齐前写入摘要；允许同一任务按旧
        # 摘要找到该 attempt，但后续仍必须通过 snapshot hash 和 scope fencing。
        legacy_input_digest = self._image_attempt_input_digest(payload)
        input_digests = tuple(dict.fromkeys((input_digest, legacy_input_digest)))
        with self.resources.factory() as session:
            previous = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt < claim.attempt_count,
                    ImageProcessingAttempt.state == "failed",
                    ImageProcessingAttempt.resume_available.is_(True),
                    ImageProcessingAttempt.target_sha256 == target_sha.lower(),
                    ImageProcessingAttempt.input_digest.in_(input_digests),
                )
                .order_by(ImageProcessingAttempt.attempt.desc())
            )
        session_id = normalize_identifier(getattr(previous, "session_id", None), kind="session") if previous else None
        executor_attempt_id = normalize_identifier(getattr(previous, "executor_attempt_id", None), kind="attempt") if previous else None
        if not session_id or not executor_attempt_id:
            return None
        selector = getattr(previous, "workspace_selector", None) if previous is not None else None
        if not isinstance(selector, str) or not SELECTOR_RE.fullmatch(selector):
            if self.scope.scope_id == "local":
                selector = "local"
            else:
                raise RuntimeError("opencode_workspace_mismatch")
        if self.scope.scope_id != "local" and selector == "local":
            raise RuntimeError("opencode_workspace_mismatch")
        if normalize_config_hash(getattr(previous, "processing_config_hash", None)) != config_hash:
            return None
        # 新任务和已装配前置器的生产任务只能复用当前 Task 已保存的 snapshot；
        # 只有没有 v2 标记且未装配前置器的旧兼容 facade 才保留历史 resume 路径。
        current_snapshot = getattr(claim, "visual_match_snapshot", None)
        if current_snapshot is None:
            if payload.get("visual_match_snapshot_protocol_version") == 2 or self._visual_snapshot_preparer is not None:
                raise RuntimeError("visual_match_snapshot_invalid")
            if any(
                getattr(previous, field, None) is not None
                for field in (
                    "visual_snapshot_sha256",
                    "visual_snapshot_protocol_version",
                    "visual_snapshot_candidate_count",
                )
            ):
                raise RuntimeError("visual_match_snapshot_invalid")
        else:
            try:
                current_summary = visual_match_snapshot_summary(
                    current_snapshot,
                    expected_sha256=getattr(claim, "visual_snapshot_sha256", None),
                )
            except VisualMatchSnapshotError as exc:
                raise RuntimeError("visual_match_snapshot_invalid") from exc
            if (
                getattr(previous, "visual_snapshot_sha256", None) != current_summary["snapshot_sha256"]
                or getattr(previous, "visual_snapshot_protocol_version", None) != current_summary["protocol_version"]
                or getattr(previous, "visual_snapshot_candidate_count", None) != current_summary["candidate_count"]
            ):
                raise RuntimeError("visual_match_snapshot_invalid")
        previous_reason = getattr(previous, "resume_reason", None) if previous is not None else None
        resume_reason = previous_reason if isinstance(previous_reason, str) and previous_reason else "session_resumable"
        return {
            "session_id": session_id,
            "executor_attempt_id": executor_attempt_id,
            "resume_of_attempt_id": executor_attempt_id,
            "resume_reason": resume_reason,
            "workspace_selector": selector,
        }

    def _begin_resume(self, claim: Task, candidate: dict[str, str]) -> bool:
        """在 claim fencing 下原子递增续跑次数，防止并发恢复器重复使用 session。"""
        now = utcnow()
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == claim.id,
                    Task.status == "running",
                    Task.claim_generation == claim.claim_generation,
                    Task.lease_owner == self.owner,
                    Task.lease_expires_at > now,
                    Task.resume_attempt_count < self.resume_max_attempts,
                )
                .with_for_update()
            )
            if task is None:
                session.commit()
                return False
            task.resume_attempt_count += 1
            task.resume_started_at = task.resume_started_at or now
            task.resume_available = False
            task.resume_reason = "resume_started"
            task.resume_session_id = candidate["session_id"]
            session.commit()
            return True

    def _image_attempt_requires_unknown(self, claim: Task) -> bool:
        """判断新 claim 前是否存在无法证明已完成的图片外部 attempt。"""
        if claim.task_type not in IMAGE_PROCESSING_TASK_TYPES or claim.attempt_count <= 1:
            return False
        # 续跑配置已启用时，额度或累计时间耗尽即使历史 attempt 行缺失也必须
        # 直接 fencing；否则数据库部分损坏会把恢复请求降级成一次新外部调用。
        if claim.task_type == "meme_context_generation" and self.resume_enabled:
            if claim.resume_attempt_count >= self.resume_max_attempts:
                return True
            if claim.resume_started_at is not None and not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
                return True
        with self.resources.factory() as session:
            previous = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt < claim.attempt_count,
                )
                .order_by(ImageProcessingAttempt.attempt.desc())
            )
            if previous is None:
                return False
            if previous.state in {"grant_committed", "external_started", "completed"}:
                return True
            # 续跑额度或累计时间耗尽后，不得退化成新的无 session 外部调用；
            # 只要历史上存在可续跑失败，就把当前 claim 收束为 unknown_execution。
            if self.resume_enabled and previous.resume_available:
                if not normalize_identifier(previous.session_id, kind="session") or not normalize_identifier(previous.executor_attempt_id, kind="attempt"):
                    return True
                if self.scope.scope_id != "local" and (
                    not isinstance(previous.workspace_selector, str)
                    or not SELECTOR_RE.fullmatch(previous.workspace_selector)
                    or previous.workspace_selector == "local"
                ):
                    # 旧 attempt 已声明可恢复但没有 workspace 绑定，不能退化为
                    # 新 session 执行；由恢复链收束为不可安全重放。
                    return True
                if claim.resume_attempt_count >= self.resume_max_attempts or not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
                    return True
            return False

    def _commit_agent_grant(self, claim: Task, payload: dict[str, Any]) -> None:
        """在 Agent 外部执行前幂等提交服务端 grant。"""
        operation_policy = getattr(self, "_operation_policy", None)
        grant_store = getattr(self, "_grant_store", None)
        if claim.task_type != "meme_context_generation" or operation_policy is None or grant_store is None:
            return
        meme_id = payload.get("meme_id")
        image_sha256 = payload.get("image_sha256")
        config_hash = normalize_config_hash(payload.get("processing_config_hash"))
        revision = payload.get("job_revision")
        policy = payload.get("reverse_image_policy") or "forbid"
        if policy not in {"forbid", "auto"}:
            raise OperationPolicyError("invalid_reverse_image_policy")
        if not isinstance(image_sha256, str) or len(image_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in image_sha256):
            raise OperationPolicyError("operation_grant_invalid")
        image_sha256 = image_sha256.lower()
        if not all(isinstance(value, str) and value for value in (meme_id, config_hash)):
            raise OperationPolicyError("operation_grant_invalid")
        mode = payload.get("submission_mode")
        if mode == "standalone":
            logical_key = payload.get("agent_grant_key")
            if not isinstance(logical_key, str) or not logical_key.startswith("standalone-agent:"):
                # protocol v2 迁移前的 standalone Task 没有 nonce/key；使用 Task ID
                # 作为同一业务任务内稳定的幂等后缀，随后把 key 写回受信 payload。
                logical_key = f"standalone-agent:{claim.id}:{meme_id}:{image_sha256}:{config_hash}:{policy}"
                payload["agent_grant_key"] = logical_key
            source = "image-processing-standalone"
        else:
            logical_key = f"agent:{meme_id}:{image_sha256}:{config_hash}:{policy}:r{revision or 'legacy'}"
            source = "image-processing"
        request = self._operation_policy.request(self.scope, Operations.ANALYSIS_AGENT, logical_key, resource_id=meme_id, task_id=claim.id, source=source, input_digest=image_sha256)
        association = None
        try:
            association = self._grant_store.get(request)
        except OperationPolicyError as exc:
            if exc.code != "operation_policy_unavailable":
                raise
            # 老 pipeline 可能先按 logical key acquire、再在 Task 创建后 bind，
            # 因而持久行仍是 task_id=NULL；只允许把这种未绑定事实绑定到当前 claim。
            unbound_request = self._operation_policy.request(
                self.scope,
                Operations.ANALYSIS_AGENT,
                logical_key,
                resource_id=meme_id,
                task_id=None,
                source=source,
                input_digest=image_sha256,
            )
            association = self._grant_store.get(unbound_request)
            if association is not None and association.state == "acquired":
                if not callable(getattr(self._grant_store, "bind_task", None)) or not self._grant_store.bind_task(association.grant, claim.id):
                    raise OperationPolicyError("operation_grant_invalid")
                association = self._grant_store.get(request)
        if association is None:
            if callable(getattr(self._grant_store, "acquire", None)):
                association = self._grant_store.acquire(request, self._operation_policy)
            else:
                grant = require_allowed(self._operation_policy.acquire(request))
                association = self._grant_store.put(GrantAssociation(request, grant))
        if association.grant.scope != self.scope or association.grant.operation != Operations.ANALYSIS_AGENT:
            raise OperationPolicyError("operation_grant_invalid")
        if callable(getattr(self._grant_store, "bind_task", None)) and association.request.task_id != claim.id:
            if not self._grant_store.bind_task(association.grant, claim.id):
                raise OperationPolicyError("operation_grant_invalid")
            association = self._grant_store.get(request) or association
        if association.state == "committed":
            return
        if association.state != "acquired":
            raise OperationPolicyError("operation_grant_invalid")
        # standalone 迁移 key 必须在 commit 前持久化，否则重启后无法按同一 grant
        # 事实恢复；兼容测试夹具没有该扩展点时仍使用当前内存 payload。
        if mode == "standalone" and callable(getattr(self, "_persist_claim_payload_updates", None)):
            self._persist_claim_payload_updates(claim, {"agent_grant_key": logical_key})
        try:
            result = self._operation_policy.commit(association.grant)
        except OperationPolicyError:
            # policy 返回异常时无法证明计量是否已经生效；保留 unknown，后续
            # claim 只能收束，不能通过重试再次触发不确定的计量边界。
            self._grant_store.transition(association.grant, "unknown")
            raise
        if not result.ok or result.state not in {"committed", "already_committed"}:
            self._grant_store.transition(association.grant, "unknown")
            raise OperationPolicyError("operation_policy_unavailable", retry_at=result.retry_at)
        if not self._grant_store.transition(association.grant, "committed"):
            self._grant_store.transition(association.grant, "unknown")
            raise OperationPolicyError("operation_grant_invalid")

    def _release_uncommitted_agent_grant(self, claim: Task, payload: Mapping[str, Any]) -> None:
        """前置阶段失败时补偿释放当前 Task 尚未提交的 Agent grant。"""
        operation_policy = getattr(self, "_operation_policy", None)
        grant_store = getattr(self, "_grant_store", None)
        if claim.task_type != "meme_context_generation" or operation_policy is None or grant_store is None:
            return
        meme_id = payload.get("meme_id")
        image_sha256 = payload.get("image_sha256")
        config_hash = normalize_config_hash(payload.get("processing_config_hash"))
        if not isinstance(image_sha256, str) or len(image_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in image_sha256):
            return
        image_sha256 = image_sha256.lower()
        if not all(isinstance(value, str) and value for value in (meme_id, config_hash)):
            return
        policy = payload.get("reverse_image_policy") or "forbid"
        if policy not in {"forbid", "auto"}:
            return
        if payload.get("submission_mode") == "standalone":
            logical_key = payload.get("agent_grant_key")
            source = "image-processing-standalone"
            if not isinstance(logical_key, str) or not logical_key.startswith("standalone-agent:"):
                return
        else:
            logical_key = f"agent:{meme_id}:{image_sha256}:{config_hash}:{policy}:r{payload.get('job_revision') or 'legacy'}"
            source = "image-processing"
        try:
            request = operation_policy.request(
                self.scope,
                Operations.ANALYSIS_AGENT,
                logical_key,
                resource_id=meme_id,
                task_id=claim.id,
                source=source,
                input_digest=image_sha256,
            )
            try:
                association = grant_store.get(request)
            except OperationPolicyError as exc:
                if exc.code != "operation_policy_unavailable":
                    raise
                # 旧 pipeline grant 可能仍未绑定 Task；释放前按同一 logical key
                # 读取该未绑定 reservation，避免预计算失败遗留可执行额度。
                unbound_request = operation_policy.request(
                    self.scope,
                    Operations.ANALYSIS_AGENT,
                    logical_key,
                    resource_id=meme_id,
                    task_id=None,
                    source=source,
                    input_digest=image_sha256,
                )
                association = grant_store.get(unbound_request)
            if association is None or association.state != "acquired":
                return
            result = operation_policy.release(association.grant)
            if not result.ok or result.state not in {"released", "already_released"}:
                grant_store.transition(association.grant, "unknown")
                return
            if not grant_store.transition(association.grant, "released"):
                grant_store.transition(association.grant, "unknown")
        except OperationPolicyError:
            # release 结果不确定时不能再次尝试 acquire；unknown 会阻止后续盲目重放。
            try:
                if "association" in locals() and association is not None:
                    grant_store.transition(association.grant, "unknown")
            except OperationPolicyError:
                pass
        except Exception:
            # 适配层无法证明补偿结果时主动收束 unknown，不能把 acquired 留给
            # 后续恢复路径，否则视觉失败可能被再次误当成可执行授权。
            try:
                if "association" in locals() and association is not None:
                    grant_store.transition(association.grant, "unknown")
            except Exception:
                pass

    def _persist_claim_payload_updates(self, claim: Task, updates: Mapping[str, Any]) -> None:
        """在当前 claim fencing 下持久化 protocol v2 迁移字段。"""
        if not updates:
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            update_payload = getattr(environment.tasks, "update_payload_fenced", None)
            if callable(update_payload) and not update_payload(claim.id, claim.claim_generation, self.owner, updates):
                raise RuntimeError("claim_expired")

    def _prepare_visual_snapshot(self, claim: Task, payload: dict[str, Any]) -> None:
        """在 Agent grant 前准备并以 claim fencing 保存视觉候选 snapshot。"""
        if claim.task_type != "meme_context_generation":
            return
        expected_sha = payload.get("image_sha256")
        meme_id = payload.get("meme_id")
        model = payload.get("visual_model")
        dimensions = payload.get("visual_dimensions")
        preprocess_version = payload.get("preprocess_version")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64 or not isinstance(meme_id, str) or not meme_id:
            raise RuntimeError("visual_match_snapshot_invalid")
        expected_sha = expected_sha.lower()
        if (
            (model is not None and (not isinstance(model, str) or not model))
            or (dimensions is not None and (not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0))
            or (preprocess_version is not None and (not isinstance(preprocess_version, str) or not preprocess_version))
        ):
            raise RuntimeError("visual_match_snapshot_invalid")
        snapshot_marker = payload.get("visual_match_snapshot_protocol_version")
        if snapshot_marker is not None and (not isinstance(snapshot_marker, int) or isinstance(snapshot_marker, bool) or snapshot_marker != 2):
            raise RuntimeError("visual_match_snapshot_invalid")
        is_resume = bool(payload.get("_resume_available") or payload.get("_resume_session_id"))
        try:
            with self.resources.environment(self.scope.scope_id) as environment:
                existing = environment.tasks.get_visual_snapshot(claim.id)
            if existing is None:
                if is_resume:
                    # 恢复路径不能因 snapshot 缺失而再次查询视觉向量；这会使
                    # 同一 session 得到不同候选并绕过原 attempt 的证据边界。
                    if snapshot_marker == 2 or self._visual_snapshot_preparer is not None:
                        raise RuntimeError("visual_match_snapshot_invalid")
                    # 没有 v2 标记和前置器的历史 facade 继续走原兼容 handler；
                    # 生产装配始终满足上面的严格分支。
                    return
                if self._visual_snapshot_preparer is None:
                    if snapshot_marker == 2:
                        raise RuntimeError("visual_match_snapshot_unavailable")
                    # 没有协议标记的历史 facade 仍保留旧兼容路径；生产新任务始终
                    # 由 scope factory 注入前置器并携带 protocol version。
                    return
                prepared = self._visual_snapshot_preparer(task_id=claim.id)
                if not isinstance(prepared, Mapping):
                    raise RuntimeError("visual_match_snapshot_invalid")
                with self.resources.environment(self.scope.scope_id) as environment:
                    if not environment.tasks.set_visual_snapshot_fenced(claim.id, claim.claim_generation, self.owner, prepared):
                        raise RuntimeError("claim_expired")
                    existing = environment.tasks.get_visual_snapshot(claim.id)
            if existing is None:
                raise RuntimeError("visual_match_snapshot_invalid")
            snapshot = validate_visual_match_snapshot(existing)
            query = snapshot.get("query")
            if (
                not isinstance(query, Mapping)
                or query.get("meme_id") != meme_id
                or str(query.get("image_sha256", "")).lower() != expected_sha.lower()
                or (model is not None and query.get("model") != model)
                or (dimensions is not None and query.get("dimensions") != dimensions)
                or (preprocess_version is not None and query.get("preprocess_version") != preprocess_version)
            ):
                raise RuntimeError("visual_match_snapshot_invalid")
            # resume 不能只相信旧 payload；Meme 或 BlobStore 在上次 attempt 后被
            # 替换时，必须在 grant 前收束为 target_changed，避免对新图片运行旧事实。
            with self.resources.environment(self.scope.scope_id) as environment:
                memes = getattr(environment, "memes", None)
                get_meme = getattr(memes, "get", None)
                if callable(get_meme):
                    current_meme = get_meme(meme_id)
                    if current_meme is None or str(getattr(current_meme, "sha256", "")).lower() != expected_sha:
                        raise RuntimeError("target_changed")
                    blob_resolver = getattr(self.resources, "blob_store_for_scope", None)
                    if callable(blob_resolver):
                        try:
                            blob = blob_resolver(self.scope.scope_id)
                            if not blob.exists_with_identity(
                                getattr(current_meme, "storage_key", None),
                                sha256=expected_sha,
                                size_bytes=getattr(current_meme, "size_bytes", None),
                            ):
                                raise RuntimeError("target_changed")
                        except RuntimeError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - 目标文件校验必须 fail-closed
                            raise RuntimeError("target_changed") from exc
            summary = visual_match_snapshot_summary(snapshot)
            # 旧任务的 payload 可能没有视觉身份字段；迁移后将 snapshot 的已
            # 校验身份放入当前 handler payload，并由 claim-fenced 更新持久迁移字段。
            payload.setdefault("visual_model", query["model"])
            payload.setdefault("visual_dimensions", query["dimensions"])
            payload.setdefault("preprocess_version", query["preprocess_version"])
            payload["processing_config_hash"] = self._processing_config_hash(payload, query)
            payload["visual_match_snapshot_protocol_version"] = 2
            payload["_visual_match_snapshot"] = snapshot
            payload["_visual_snapshot_sha256"] = summary["snapshot_sha256"]
            payload["_visual_snapshot_protocol_version"] = summary["protocol_version"]
            payload["_visual_snapshot_matched_at"] = datetime.fromisoformat(str(summary["matched_at"]).replace("Z", "+00:00"))
            payload["_visual_snapshot_candidate_count"] = summary["candidate_count"]
            self._persist_claim_payload_updates(
                claim,
                {
                    "visual_model": payload["visual_model"],
                    "visual_dimensions": payload["visual_dimensions"],
                    "preprocess_version": payload["preprocess_version"],
                    "processing_config_hash": payload["processing_config_hash"],
                    "visual_match_snapshot_protocol_version": 2,
                },
            )
            if self._visual_candidate_preparer is not None:
                try:
                    self._visual_candidate_preparer(claim=claim, payload=payload, snapshot=snapshot)
                except RuntimeError as exc:
                    code = getattr(exc, "code", None) or str(exc).partition(":")[0]
                    if code == "claim_expired":
                        raise
                    raise RuntimeError("visual_candidate_materialization_failed") from exc
                except Exception as exc:  # noqa: BLE001 - 物化适配层失败必须阻止 grant
                    raise RuntimeError("visual_candidate_materialization_failed") from exc
        except (DatabaseError, VisualMatchSnapshotError) as exc:
            code = getattr(exc, "code", "visual_match_snapshot_invalid")
            raise RuntimeError(str(code)) from exc
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 视觉适配层必须暴露稳定 code
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in STABLE_TASK_ERRORS:
                raise RuntimeError(code) from exc
            raise

    def submit(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        schedule: bool = True,
        _lane: str | None = None,
        _lane_backpressure: int | None = None,
        _lane_backpressure_scope: str | None = None,
        _lane_resource_key: str | None = None,
    ) -> TaskRecord:
        """以事务插入或复用活动任务，并立即安排本进程执行。

        图片阶段来源由当前受信控制面 payload 规范化后落入专用列；客户端不能
        通过额外 scope、Job、grant 或 claim 字段改变这些事实。
        """
        payload = dict(payload or {})
        for field in UNTRUSTED_SCOPE_FIELDS:
            payload.pop(field, None)
        # scope/user 只能由 resolver 或 Task.scope_id 提供；即使调用方伪造字段，
        # 也不得让它们进入后续 handler 作为授权事实。
        payload.pop("scope_id", None)
        payload.pop("user_id", None)
        # session/attempt 只能由当前 Worker 从持久 attempt 恢复，客户端 payload
        # 即使携带同名字段也不得改变续跑绑定事实。
        for internal_field in ("session_id", "executor_attempt_id", "attempt_id", "resume_available", "resume_reason"):
            payload.pop(internal_field, None)
        # 资源归属由受信控制面通过私有参数传入，客户端 payload 不能覆盖持久事实。
        payload.pop("lane_resource_key", None)
        lane = _lane if _lane is not None else ("agent" if task_type == "meme_context_generation" else "default")
        image_stage = None
        submission_mode = None
        processing_job_id = None
        if task_type in IMAGE_PROCESSING_TASK_TYPES:
            stage_by_type = {
                "visual_embedding_generation": "visual",
                "meme_context_generation": "agent",
                "image_auto_rename": "auto_rename",
                "text_embedding_generation": "text_embedding",
            }
            expected_stage = stage_by_type[task_type]
            requested_stage = payload.get("stage")
            if requested_stage is not None and requested_stage != expected_stage:
                raise RuntimeError("image_stage_mismatch")
            requested_mode = payload.get("submission_mode")
            if requested_mode not in {"pipeline", "standalone"}:
                # 没有新来源字段的旧记录仍可被读取/执行，查询时会显示为未归类；
                # 新控制面入口始终显式传入 mode。
                requested_mode = None
            submission_mode = requested_mode
            if submission_mode == "pipeline":
                raw_job_id = payload.get("job_id")
                if not isinstance(raw_job_id, str) or not raw_job_id:
                    raise RuntimeError("image_processing_job_required")
                processing_job_id = raw_job_id
            elif submission_mode == "standalone":
                if payload.get("job_id") is not None:
                    raise RuntimeError("image_task_job_conflict")
            elif payload.get("job_id") is not None:
                # 旧 job 叶子在来源迁移前仍按 pipeline 处理，避免丢失父 Job
                # 关联；该分支只接受服务端已有的 Job UUID。
                submission_mode = "pipeline"
                processing_job_id = payload.get("job_id")
            # 没有阶段、Job 或来源字段的旧 facade 调用属于迁移前任务。保留
            # NULL image_stage 使它可以继续完成既有业务；迁移脚本对能够
            # 可靠识别阶段的历史任务会补写 image_stage，专用 Worker 随后
            # 将那类未归类任务收束为只读诊断。
            explicit_source = requested_stage is not None or requested_mode is not None or processing_job_id is not None
            if explicit_source:
                image_stage = expected_stage
                payload["stage"] = expected_stage
                if submission_mode is not None:
                    payload["submission_mode"] = submission_mode
        dedupe = self._dedupe(task_type, payload)
        if task_type == "meme_context_generation":
            self._context_policy_conflict(payload, dedupe)
        with self.resources.environment(self.scope.scope_id) as environment:
            try:
                record = environment.tasks.submit(task_type=task_type, payload=payload, lane=lane, dedupe_key=dedupe, settings_version=self.settings_version, max_attempts=self.max_attempts, lane_backpressure=_lane_backpressure, lane_backpressure_scope_id=_lane_backpressure_scope, lane_resource_key=_lane_resource_key, submission_mode=submission_mode, image_stage=image_stage, processing_job_id=processing_job_id)
            except DatabaseError as exc:
                if exc.code == "agent_backpressure":
                    existing = environment.uow.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe, Task.status.in_(("queued", "running"))))
                    if existing:
                        record = existing
                    else:
                        raise RuntimeError("thumbnail_backpressure" if lane == "thumbnail" else "agent_backpressure") from exc
                else:
                    raise
            if task_type == "meme_context_generation":
                self._assert_context_policy(record, payload)
            if task_type in {"meme_context_generation", "visual_embedding_generation"} and isinstance(payload.get("batch_id"), str):
                environment.tasks.add_batch_item(payload["batch_id"], record.id)
            snapshot = self._record_to_dataclass(record)
        if schedule:
            self._schedule(snapshot.task_id)
        return snapshot

    def submit_thumbnail(self, payload: dict[str, Any], *, backpressure: int) -> TaskRecord:
        """在独立缩略图 lane 中原子提交任务，并按当前 scope 执行背压。"""
        return self.submit(
            "derived_thumbnail_generation",
            payload,
            _lane="thumbnail",
            _lane_backpressure=backpressure,
            _lane_backpressure_scope=self.scope.scope_id,
        )

    def retry(self, task_id: str) -> TaskRecord:
        """重试一个普通失败任务；图片阶段必须通过受限图片入口重试。"""
        record = self.get(task_id)
        if record is None:
            raise RuntimeError("task_not_found")
        if record.task_type in IMAGE_PROCESSING_TASK_TYPES:
            raise RuntimeError("image_stage_retry_forbidden")
        if record.status != "failed":
            raise RuntimeError("task_not_failed")
        payload = {key: value for key, value in record.payload.items() if not key.startswith("_claim_")}
        # 失败任务的同一 dedupe key 已不属于活动集合，submit 会创建新的可轮询尝试。
        return self.submit(record.task_type, payload, _lane_resource_key=record.lane_resource_key)

    def schedule(self, task_id: str) -> None:
        """显式唤醒一个已提交任务；批量提交时用于关闭入批竞态窗口。"""
        self._schedule(task_id)

    def _schedule(self, task_id: str) -> None:
        """避免同一进程重复调度同一个数据库任务。"""
        if self._worker_manager is not None:
            self._worker_manager.schedule(task_id)
            return
        with self._lock:
            if self._stopped.is_set() or task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._executor.submit(self._run, task_id)

    def _run(self, task_id: str, *, preclaimed: Task | None = None) -> None:
        """执行已认领任务并以 claim generation fencing 写回终态。"""
        claim = preclaimed
        try:
            if claim is None:
                with self.resources.environment(self.scope.scope_id) as environment:
                    queued_record = environment.tasks.get(task_id)
                    if queued_record is None:
                        return
                    lane = queued_record.lane
                    claim = environment.tasks.claim(
                        owner=self.owner,
                        lease_seconds=self.lease_seconds,
                        task_id=task_id,
                        lane=lane,
                        lane_capacity=self.agent_concurrency if lane == "agent" else None,
                        resource_key=queued_record.lane_resource_key if lane == "agent" else None,
                        resource_capacity=self.resource_capacity(queued_record.lane_resource_key) if lane == "agent" else None,
                        scope_capacity=self.agent_scope_concurrency if lane == "agent" else None,
                        # 专用 facade 必须能恢复自己的过期图片叶子；通用 manager
                        # 在更早的 ``_claim_for_task`` 边界排除这些类型。
                        exclude_task_types=None,
                    )
                    if claim is None or claim.id != task_id:
                        return
                    task_payload = dict(claim.payload or {})
                    generation = claim.claim_generation
                    task_payload["_claim_task_id"] = claim.id
                    task_payload["_claim_generation"] = generation
                    task_payload["_claim_owner"] = self.owner
                    task_payload["_claim_attempt"] = claim.attempt_count
                    task_payload["_resume_attempt_count"] = claim.resume_attempt_count
                    task_payload["_resume_started_at"] = claim.resume_started_at
                    try:
                        claim_scope = ScopeContext(claim.scope_id)
                    except (TypeError, ValueError) as exc:
                        # 无效持久 scope 不能猜测为 local；以稳定错误进入 fencing 收束。
                        environment.tasks.fail_fenced(task_id, generation, self.owner, message="任务 scope 无效", error={"error": "task_scope_invalid", "message": "任务缺少有效 scope"}, retry=False)
                        raise RuntimeError("task_scope_invalid") from exc
                    if claim_scope.scope_id != self.scope.scope_id:
                        environment.tasks.fail_fenced(task_id, generation, self.owner, message="任务 scope 与 Worker 不一致", error={"error": "task_scope_mismatch", "message": "任务 scope 与当前执行环境不一致"}, retry=False)
                        raise RuntimeError("task_scope_mismatch")
                    task_payload["_claim_scope_id"] = claim_scope.scope_id
            else:
                if claim.id != task_id or claim.scope_id != self.scope.scope_id:
                    return
                task_payload = dict(claim.payload or {})
                generation = claim.claim_generation
                task_payload["_claim_task_id"] = claim.id
                task_payload["_claim_generation"] = generation
                task_payload["_claim_owner"] = self.owner
                task_payload["_claim_attempt"] = claim.attempt_count
                task_payload["_resume_attempt_count"] = claim.resume_attempt_count
                task_payload["_resume_started_at"] = claim.resume_started_at
                try:
                    claim_scope = ScopeContext(claim.scope_id)
                except (TypeError, ValueError):
                    return
                if claim_scope.scope_id != self.scope.scope_id:
                    return
                task_payload["_claim_scope_id"] = claim_scope.scope_id
            handler = self._worker_manager.handler(claim.task_type) if self._worker_manager is not None else self._handlers.get(claim.task_type)
            if handler is None:
                self._fenced_failure(task_id, generation, message="任务处理器不可用", error={"error": "task_handler_missing", "message": "当前服务未注册此任务类型"}, retry=False)
                return

            if claim.task_type in IMAGE_PROCESSING_TASK_TYPES and claim.submission_mode not in {"pipeline", "standalone"} and claim.image_stage is not None:
                # 无法可靠归类的历史图片 Task 只允许查询诊断，不能在启动恢复时
                # 被旧 Worker 重新执行或通过异常路径产生下游阶段。没有显式
                # 阶段列的迁移前兼容任务仍由原任务 facade 完成。
                self._fenced_failure(
                    task_id,
                    generation,
                    message="历史图片任务未归类，只读展示",
                    error={"error": "image_task_unclassified", "message": "历史图片阶段缺少可信提交来源"},
                    retry=False,
                )
                return

            if self._image_attempt_requires_unknown(claim):
                self._image_attempt_state(claim, task_payload, "unknown_execution")
                self._fenced_failure(
                    task_id,
                    generation,
                    message="外部执行结果无法确认",
                    error={"error": "unknown_execution", "message": "上一次图片阶段已进入外部执行窗口，无法安全重放"},
                    retry=False,
                    resume_available=False,
                    resume_reason="unknown_execution",
                )
                return

            try:
                resume_candidate = self._resume_candidate(claim, task_payload)
            except RuntimeError as exc:
                code = str(exc).partition(":")[0]
                if code not in {
                    "opencode_workspace_mismatch",
                    "visual_match_snapshot_invalid",
                    "visual_match_snapshot_conflict",
                    "visual_match_snapshot_unavailable",
                }:
                    raise
                self._image_attempt_state(claim, task_payload, "failed")
                self._fenced_failure(
                    task_id,
                    generation,
                    message="视觉候选 snapshot 无法恢复" if code.startswith("visual_match_snapshot") else "workspace 绑定无法恢复",
                    error={"error": code, "message": "视觉候选 snapshot 与持久恢复事实不一致" if code.startswith("visual_match_snapshot") else "workspace selector 与持久恢复事实不一致"},
                    retry=False,
                    resume_available=False,
                    resume_reason=code,
                )
                return
            if resume_candidate is not None:
                if not self._begin_resume(claim, resume_candidate):
                    # 恢复计数的原子 fencing 失败时不能降级为一次全新外部调用；
                    # 让当前 claim 自然收束，由仍有效的 Worker 重新决定。
                    return
                # session 只来自上一条同 scope/Task/输入摘要的 attempt，不能从
                # 普通 payload 或客户端请求直接注入。
                task_payload["_resume_session_id"] = resume_candidate["session_id"]
                task_payload["_resume_of_attempt_id"] = resume_candidate["resume_of_attempt_id"]
                task_payload["_previous_executor_attempt_id"] = resume_candidate["executor_attempt_id"]
                # 候选已通过全部恢复绑定校验；即使下一次 executor 错误没有回传
                # session，也必须保留这份服务端确认的可续跑事实交给 handler。
                task_payload["_resume_available"] = True
                task_payload["_resume_reason"] = resume_candidate["resume_reason"]
                task_payload["_workspace_selector"] = resume_candidate["workspace_selector"]

            self._image_attempt_state(claim, task_payload, "prepared")

            def progress(value: float | None, message: str | None = None) -> None:
                """把 handler 进度写回当前 claim，失败时由 fencing 规则拒绝旧写回。"""
                self._fenced_update(task_id, generation, progress=value, message=message)

            heartbeat_stop = Event()
            def heartbeat() -> None:
                """定期续租当前 claim，停止后退出后台线程。"""
                while not heartbeat_stop.wait(max(1, self.lease_seconds // 3)):
                    with self.resources.environment(self.scope.scope_id) as heartbeat_env:
                        if not heartbeat_env.tasks.heartbeat(task_id, generation, self.owner, self.lease_seconds):
                            return
            heartbeat_thread = threading.Thread(target=heartbeat, name=f"mememeow-heartbeat-{task_id}", daemon=True)
            heartbeat_thread.start()
            try:
                if claim.task_type in IMAGE_PROCESSING_TASK_TYPES:
                    if claim.task_type == "meme_context_generation":
                        # 视觉候选失败发生在 grant 和 external_started 之前，不能被
                        # 外部执行恢复逻辑误判为 unknown_execution。
                        self._prepare_visual_snapshot(claim, task_payload)
                        # 先把已校验的 snapshot 摘要绑定到当前 attempt；即使后续
                        # grant 或 OpenCode 失败，恢复器也只能复用这一份事实。
                        self._image_attempt_state(claim, task_payload, "prepared")
                    # 图片阶段均可能触发外部模型或持久副作用；Agent 先完成
                    # grant commit，再进入外部执行窗口，恢复者才能区分计量边界。
                    self._commit_agent_grant(claim, task_payload)
                    task_payload["_agent_grant_committed"] = True
                    if claim.task_type == "meme_context_generation":
                        self._image_attempt_state(claim, task_payload, "grant_committed")
                    # 恢复者无法证明结果时必须收束 unknown_execution。
                    self._image_attempt_state(claim, task_payload, "external_started")
                result = handler(task_payload, progress)
            except Exception as exc:  # noqa: BLE001
                if not task_payload.get("_agent_grant_committed"):
                    self._release_uncommitted_agent_grant(claim, task_payload)
                if isinstance(exc, OperationPolicyError):
                    code = exc.code
                    diagnostic = code
                else:
                    diagnostic = str(exc)[:500]
                    exception_code = getattr(exc, "code", None)
                    code = exception_code if isinstance(exception_code, str) and exception_code in STABLE_TASK_ERRORS else diagnostic.partition(":")[0] if diagnostic.partition(":")[0] in STABLE_TASK_ERRORS else "task_failed"
                resume_available = bool(task_payload.get("_resume_available"))
                resume_reason = task_payload.get("_resume_reason") if isinstance(task_payload.get("_resume_reason"), str) else None
                session_id = task_payload.get("_resume_session_id") if isinstance(task_payload.get("_resume_session_id"), str) else None
                executor_attempt_id = task_payload.get("_executor_attempt_id") if isinstance(task_payload.get("_executor_attempt_id"), str) else None
                if claim.task_type == "meme_context_generation" and agent_failure_requires_unknown(
                    code,
                    session_id=session_id,
                    resume_available=resume_available,
                    resuming=isinstance(task_payload.get("_resume_session_id"), str),
                    resume_enabled=self.resume_enabled,
                ):
                    # handler 已尽力记录原始 executor/provider 错误；任务终态必须
                    # 另行收束为 unknown_execution，阻止同一业务任务从头重放。
                    original_code = code
                    code = "unknown_execution"
                    diagnostic = f"外部执行状态无法确认（{original_code}）"
                    resume_available = False
                    if resume_reason != "resume_budget_exhausted":
                        resume_reason = "unknown_execution"
                self._image_attempt_state(claim, task_payload, "unknown_execution" if code in {"unknown_execution", "reverse_image_unknown_execution"} else "failed")
                retry_delay = bounded_backoff(
                    claim.resume_attempt_count,
                    base_seconds=self.resume_backoff_seconds if self.resume_enabled and resume_available else 0,
                    max_seconds=self.resume_max_backoff_seconds,
                )
                retry = code not in {
                    "target_changed",
                    "agent_output_schema_invalid",
                    "agent_output_invalid_json",
                    "agent_result_file_missing",
                    "agent_result_file_unreadable",
                    "agent_result_file_too_large",
                    "agent_result_file_invalid_json",
                    "agent_result_file_schema_invalid",
                    "agent_image_path_forbidden",
                    "agent_input_provider_unavailable",
                    "agent_result_path_invalid",
                    "task_handler_missing",
                    "opencode_not_configured",
                    "agent_runtime_unavailable",
                    "agent_image_root_mismatch",
                    "reverse_image_forbidden",
                    "invalid_reverse_image_policy",
                    "usage_request_conflict",
                    "visual_model_not_configured",
                    "visual_model_migration_required",
                    "visual_model_identity_invalid",
                    "visual_weights_checksum_mismatch",
                    "visual_embedding_dimensions_mismatch",
                    "visual_embedding_non_finite",
                    "visual_embedding_zero_norm",
                    "visual_image_decode_failed",
                    "visual_model_identity_mismatch",
                    "visual_service_invalid_response",
                    "visual_embedding_invalid",
                    "visual_embedding_sha256_invalid",
                    "visual_embedding_sha256_mismatch",
                    "embedding_not_configured",
                    "embedding_dimensions_mismatch",
                    "embedding_non_finite",
                    "embedding_zero_norm",
                    "query_embedding_not_ready",
                    "visual_match_snapshot_invalid",
                    "visual_match_snapshot_conflict",
                    "visual_match_snapshot_unavailable",
                    "visual_candidate_materialization_failed",
                    "claim_expired",
                    "invalid_task",
                    "task_not_running",
                    "auto_rename_title_missing",
                    "auto_rename_invalid_filename",
                    "auto_rename_target_exists",
                    "auto_rename_target_changed",
                    "auto_rename_claim_expired",
                    "auto_rename_unknown_execution",
                    "task_scope_invalid",
                    "task_scope_mismatch",
                    "unknown_execution",
                    "reverse_image_unknown_execution",
                    "operation_forbidden",
                    "operation_limit_exceeded",
                    "operation_policy_unavailable",
                    "operation_grant_invalid",
                    "blocked",
                }
                audit_result = self._with_reverse_image_audit(task_id, None, write_provenance=False)
                self._fenced_failure(
                    task_id,
                    generation,
                    message="任务执行失败",
                    error={"error": code, "message": diagnostic},
                    retry=retry,
                    result=audit_result,
                    retry_delay_seconds=retry_delay,
                    resume_available=resume_available,
                    resume_reason=resume_reason,
                    session_id=session_id,
                    executor_attempt_id=executor_attempt_id,
                )
            else:
                # 只有当前 claim 仍有效时才写入任务终态和 Meme provenance。
                self._image_attempt_state(claim, task_payload, "completed")
                audit_result = self._with_reverse_image_audit(task_id, result, write_provenance=False)
                self._fenced_success(task_id, generation, audit_result)
            finally:
                heartbeat_stop.set()
            self._maybe_finalize(task_id)
        finally:
            if self._worker_manager is not None:
                self._worker_manager._task_finished(task_id, claimed=claim is not None)
            else:
                with self._lock:
                    self._scheduled.discard(task_id)
                if claim is not None and not self._stopped.is_set():
                    self._schedule_queued()

    def _schedule_queued(self) -> None:
        """在槽位释放后唤醒数据库中的排队任务，避免 lane 满载时忙循环。"""
        if self._worker_manager is not None:
            self._worker_manager._schedule_queued()
            return
        if self._stopped.is_set():
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            records, _ = environment.tasks.list(statuses={"queued"}, limit=100)
        for record in records:
            if (
                (not _is_explicit_image_task(record))
                if self._finalize_image_tasks
                else record.task_type in IMAGE_PROCESSING_TASK_TYPES
            ):
                self._schedule(record.id)

    def _fenced_update(self, task_id: str, generation: int, **changes: Any) -> bool:
        """在一个短事务中验证 owner/generation/租约后更新任务。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.tasks.update_fenced(task_id, generation, self.owner, **changes)

    def _fenced_success(self, task_id: str, generation: int, result: Any) -> bool:
        """以 claim fencing 原子提交成功结果和图片 Agent provenance。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            complete = getattr(environment.tasks, "complete_fenced_with_provenance", None)
            if callable(complete):
                return bool(complete(task_id, generation, self.owner, result=result))
            # 兼容尚未提供原子扩展的宿主 repository；标准 PostgreSQL
            # repository 始终走上面的单事务路径。
            changed = environment.tasks.update_fenced(
                task_id,
                generation,
                self.owner,
                status="succeeded",
                progress=1.0,
                message="任务完成",
                result=result,
            )
        if changed:
            self._write_reverse_image_provenance(task_id, generation)
        return changed

    def _fenced_failure(self, task_id: str, generation: int, *, message: str, error: dict[str, Any], retry: bool, result: Any | None = None, retry_delay_seconds: int = 0, resume_available: bool | None = None, resume_reason: str | None = None, session_id: str | None = None, executor_attempt_id: str | None = None) -> bool:
        """按最大尝试次数将当前 claim 重新排队或置为失败。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            changed, _should_retry = environment.tasks.fail_fenced(
                task_id,
                generation,
                self.owner,
                error=error,
                message=message,
                retry=retry,
                result=result,
                retry_delay_seconds=retry_delay_seconds,
                resume_available=resume_available,
                resume_reason=resume_reason,
                session_id=session_id,
                executor_attempt_id=executor_attempt_id,
            )
        # 当前执行线程的 ``finally`` 会在释放本地调度标记后统一扫描 queued
        # 任务。这里不能提前按 task_id 单独提交新的 future：preclaimed 的
        # 兼容调用可能尚未登记调度标记，会让同一任务在旧 claim 收束事务刚
        # 完成后被重复认领，既造成恢复竞态，也让调用方无法观察到 queued 快照。
        return changed

    def _with_reverse_image_audit(self, task_id: str, result: Any, *, write_provenance: bool = False) -> dict[str, Any]:
        """按任务事件生成终态审计摘要，默认不修改 Meme provenance。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            task = environment.tasks.get(task_id)
            policy = str((task.payload or {}).get("reverse_image_policy") or "forbid") if task else "forbid"
            audit = environment.reverse_image_usage.aggregate_task(task_id)
            payload = dict(result) if isinstance(result, dict) else {}
            payload["reverse_image"] = {"policy": policy, **audit}
            if write_provenance:
                meme_id = (task.payload or {}).get("meme_id") if task else None
                if meme_id:
                    self._write_reverse_image_provenance(task_id, None)
            return payload

    def _write_reverse_image_provenance(self, task_id: str, claim_generation: int | None) -> None:
        """仅在成功 claim 的收束阶段写回 Meme 反向图片审计，避免旧 Worker 覆盖新结果。"""
        try:
            with self.resources.environment(self.scope.scope_id) as environment:
                task = environment.tasks.get(task_id)
                if task is None or task.status != "succeeded":
                    return
                if claim_generation is not None and task.claim_generation != claim_generation:
                    return
                audit = environment.reverse_image_usage.aggregate_task(task_id)
                meme_id = (task.payload or {}).get("meme_id")
                if not meme_id:
                    return
                meme = environment.memes.get(meme_id, for_update=True)
                if meme is None:
                    return
                provenance = dict(meme.provenance or {})
                provenance["reverse_image"] = {"policy": str((task.payload or {}).get("reverse_image_policy") or "forbid"), **audit}
                meme.provenance = provenance
                environment.uow.session.flush()
        except Exception:
            # 审计 provenance 是可重建的附属写回，不能让已成功的任务线程崩溃。
            return

    def get(self, task_id: str) -> TaskRecord | None:
        """读取当前 scope 任务快照。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.tasks.get(task_id)
            if record is None:
                return None
            slot = environment.tasks.slot_for_task(record.id)
            return self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None)

    def claim_next(self, *, owner: str | None = None, lease_seconds: int | None = None, lane: str = "agent", resource_key: str | None = None, resource_capacity: int | None = None) -> TaskRecord | None:
        """调用 PostgreSQL 公平 claim 并返回带可信 scope 的安全任务快照。

        正常进程级 manager 直接使用同一 repository 入口；此 facade 方法只为
        宿主适配层和诊断工具保留，不从 payload 接受 scope 或 user 字段。
        """
        with self.resources.environment(self.scope.scope_id) as environment:
            claim = environment.tasks.claim_next(
                owner=owner or self.owner,
                lease_seconds=lease_seconds or self.lease_seconds,
                lane=lane,
                lane_capacity=self.agent_concurrency,
                resource_key=resource_key,
                resource_capacity=resource_capacity if resource_capacity is not None else self.resource_capacity(resource_key),
                scope_capacity=self.agent_scope_concurrency,
            )
            if claim is None:
                return None
            slot = environment.tasks.slot_for_task(claim.id)
            return self._record_to_dataclass(claim, slot_id=slot.slot_number if slot else None)

    def find_active(self, task_type: str, dedupe_key: str) -> TaskRecord | None:
        """按当前 scope、类型和活动去重键读取叶子 Task。

        图片 Worker 在取得 Agent grant 前调用此方法，避免把已有活动任务误判为
        新的计量请求；查询结果只是提示，真正提交仍由 TaskRepository 的唯一键兜底。
        """
        if not isinstance(task_type, str) or not task_type or not isinstance(dedupe_key, str) or not dedupe_key:
            return None
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.uow.session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.task_type == task_type,
                    Task.dedupe_key == dedupe_key,
                    Task.status.in_(("queued", "running")),
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
            )
            if record is None:
                return None
            slot = environment.tasks.slot_for_task(record.id)
            return self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None)

    def cancel(self, task_id: str) -> bool:
        """取消单个任务并仅终止其 Agent session，不停止共享容器。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.tasks.get(task_id, for_update=True)
            if record is None:
                return False
            changed = environment.tasks.cancel(task_id, error={"error": "task_cancelled", "message": "任务已取消"}, message="任务已取消")
        if changed:
            handler = self._handlers.get(record.task_type)
            _ = handler  # 仅保留任务类型快照，实际 session 清理由运行器按 task_id 完成。
        return changed

    def list(self, *, statuses: set[str] | None = None, task_types: set[str] | None = None, cursor: str | None = None, limit: int = 50) -> tuple[list[TaskRecord], str | None]:
        """分页列出当前 scope 任务，返回兼容 TaskRecord 的安全快照。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            records, next_cursor = environment.tasks.list(statuses=statuses, task_types=task_types, cursor=cursor, limit=limit)
            result = []
            for record in records:
                slot = environment.tasks.slot_for_task(record.id)
                result.append(self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None))
            return result, next_cursor

    def _maybe_finalize(self, task_id: str) -> None:
        """批次成员全部终态后在数据库中只提交一次 finalizer 标记。"""
        record = self.get(task_id)
        if not record or record.task_type not in {"meme_context_generation", "visual_embedding_generation"}:
            return
        if not self._finalize_image_tasks:
            return
        # 批量接口可能复用上传时已存在的活动去重任务；此时 payload 没有
        # batch_id，必须以数据库关联表为准，避免 finalizer 永久遗漏。
        with self.resources.environment(self.scope.scope_id) as environment:
            values = environment.tasks.batch_ids_for_task(task_id)
        batch_id = record.payload.get("batch_id")
        batch_ids = record.payload.get("batch_ids")
        if isinstance(batch_id, str) and batch_id:
            values.append(batch_id)
        if isinstance(batch_ids, list):
            values.extend(item for item in batch_ids if isinstance(item, str) and item)
        values = list(dict.fromkeys(values))
        if not values:
            return
        for current_batch_id in values:
            with self.resources.environment(self.scope.scope_id) as environment:
                task = environment.tasks.finalize_batch_with_task(
                    current_batch_id,
                    task_type="cache_generation",
                    payload={},
                    dedupe_key="cache_generation",
                    settings_version=self.settings_version,
                    max_attempts=self.max_attempts,
                )
                created_task_id = task.id if task is not None else None
            if created_task_id:
                self._schedule(created_task_id)
            if task is None or not self._batch_finalizer:
                continue
            try:
                self._batch_finalizer(current_batch_id)
            except Exception:
                pass

    def shutdown(self) -> None:
        """停止新认领并将本 Worker 仍持有的任务标记为可诊断中断。"""
        if self._worker_manager is not None:
            return
        self._stopped.set()
        with self.resources.environment(self.scope.scope_id) as environment:
            environment.tasks.interrupt_owner(self.owner)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
