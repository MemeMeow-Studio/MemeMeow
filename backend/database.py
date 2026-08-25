"""PostgreSQL 权威存储、scope 边界和同步数据环境。

该模块位于 API、任务 Worker 与领域服务之间。所有结构化业务数据通过这里创建的
SQLAlchemy Session 和已绑定 scope 的 repository 访问；图片字节仍由 BlobStore 保存。
"""

from __future__ import annotations

import hashlib
import json
import os
import errno
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Sequence
from uuid import UUID

from sqlalchemy import (
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.storage_security import StorageRootError, validate_controlled_root
from backend.agent_resume import append_error_history, append_task_error_history, normalize_identifier, sanitize_error
from executor.agent_limits import validate_agent_concurrency
from backend.persistence.engine import (
    CURRENT_SCHEMA_REVISION,
    DatabaseError,
    SCOPE_LOCAL,
    check_database,
    create_engine_for_settings,
    create_engine_for_url,
    database_url_from_env,
    ensure_optional_control_schema,
    initialize_local,
)
from backend.persistence.models import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    DeclarativeBase,
    EMBEDDING_DIMENSIONS,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    JSONB,
    Mapped,
    String,
    UniqueConstraint,
    Uuid,
    Vector,
    VISUAL_EMBEDDING_DIMENSIONS,
    UTC,
    OPTIONAL_CONTROL_TABLES,
    AgentCallbackRequest,
    Base,
    ImageProcessingAttempt,
    ImageProcessingJob,
    ImageProcessingStage,
    InstallationState,
    Meme,
    MemeCollection,
    MemeCollectionItem,
    MemeEmbedding,
    MemeTextEmbedding,
    MemeVisualEmbedding,
    OperationGrant,
    ReverseImageUsageEvent,
    Scope,
    ScopeContext,
    SearchGeneration,
    SearchHead,
    SearchMigrationState,
    StorageOperation,
    Task,
    TaskBatch,
    TaskBatchItem,
    TaskLaneFairness,
    TaskLaneSlot,
    mapped_column,
    timezone,
    unicodedata,
    utcnow,
)
from backend.persistence.resources import DataEnvironment, DatabaseResources
from backend.persistence.repositories.collections import CollectionRepository
from backend.persistence.repositories.memes import MemeRepository
from backend.persistence.repositories.search import SearchRepository
from backend.persistence.repositories.visual_embeddings import VisualEmbeddingRepository, validate_visual_vector
from backend.persistence.unit_of_work import UnitOfWork


# 图片 pipeline 的显式阶段任务由专用控制面推进；通用 Agent fair claim 不应抢走
# 这些带可信 submission_mode 的叶子任务。这里复制稳定协议集合，避免 database.py
# 与任务执行模块互相导入。
IMAGE_PROCESSING_LANE_TYPES = frozenset(
    {
        "visual_embedding_generation",
        "meme_context_generation",
        "image_auto_rename",
        "text_embedding_generation",
    }
)


def _validate_lane_capacities(lane_capacity: object | None, scope_capacity: object | None) -> tuple[int | None, int | None]:
    """校验数据库公平调度的 lane/scope 容量，不对非法值做静默收敛。"""
    if lane_capacity is None:
        return None, None
    try:
        capacity = validate_agent_concurrency(lane_capacity)
        limit = validate_agent_concurrency(scope_capacity, backpressure=capacity) if scope_capacity is not None else None
    except ValueError as exc:
        raise DatabaseError("agent_claim_config_invalid") from exc
    return capacity, limit


class TaskRepository:
    """数据库任务队列 repository，所有方法自动带 scope 条件。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        """按当前 scope 读取任务，不泄露其他 scope 记录。"""
        statement = select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def submit(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        lane: str = "default",
        dedupe_key: str | None = None,
        settings_version: str | None = None,
        max_attempts: int = 3,
        task_id: str | None = None,
        lane_backpressure: int | None = None,
        submission_mode: str | None = None,
        image_stage: str | None = None,
        processing_job_id: UUID | str | None = None,
    ) -> Task:
        """在事务中插入或复用活动任务，并保存图片提交来源事实。

        ``submission_mode`` 等参数只应由图片控制面传入。为兼容无法回填来源的
        历史测试任务，未提供来源时保留 NULL，但任何带来源的新图片任务都会在
        这里校验阶段、Job 关联和模式互斥关系。
        """
        image_task_stages = {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
            "text_embedding_generation": "text_embedding",
        }
        expected_stage = image_task_stages.get(task_type)
        if expected_stage is not None:
            submission_mode = submission_mode or (str(payload.get("submission_mode")) if payload.get("submission_mode") in {"pipeline", "standalone"} else None)
            if submission_mode not in {None, "pipeline", "standalone"}:
                raise DatabaseError("image_submission_mode_invalid")
            requested_stage = payload.get("stage")
            if requested_stage is not None and requested_stage != expected_stage:
                raise DatabaseError("image_stage_mismatch")
            candidate_job = processing_job_id or payload.get("job_id")
            if candidate_job is not None:
                try:
                    processing_job_id = UUID(str(candidate_job))
                except (TypeError, ValueError) as exc:
                    raise DatabaseError("image_processing_job_invalid") from exc
            # 无来源且无显式阶段的旧任务保留 NULL 阶段，供迁移脚本区分
            # “仍可兼容执行的旧 facade 任务”和“已经明确为历史图片阶段”的记录。
            explicit_source = image_stage is not None or requested_stage is not None or submission_mode is not None or processing_job_id is not None
            if explicit_source:
                image_stage = image_stage or (str(requested_stage) if requested_stage is not None else expected_stage)
            else:
                image_stage = None
            if image_stage is not None and image_stage != expected_stage:
                raise DatabaseError("image_stage_mismatch")
            if submission_mode == "pipeline" and processing_job_id is None:
                raise DatabaseError("image_processing_job_required")
            if submission_mode == "standalone" and processing_job_id is not None:
                raise DatabaseError("image_task_job_conflict")
            if submission_mode == "standalone" and payload.get("job_id") is not None:
                raise DatabaseError("image_task_job_conflict")
        # Agent 任务先锁定 lane，再检查活动去重，避免策略请求并发穿过预检窗口。
        if lane == "agent" or lane_backpressure is not None:
            self._lock_lane(lane)
        if dedupe_key:
            existing = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))).with_for_update())
            if existing:
                return existing
        if lane_backpressure is not None:
            active = self.session.scalar(select(func.count()).select_from(Task).where(Task.lane == lane, Task.status.in_(("queued", "running")))) or 0
            if int(active) >= int(lane_backpressure):
                raise DatabaseError("agent_backpressure")
        task = Task(
            id=task_id or uuid.uuid4().hex,
            scope_id=self.scope.scope_id,
            task_type=task_type,
            submission_mode=submission_mode,
            image_stage=image_stage,
            processing_job_id=processing_job_id,
            lane=lane,
            payload=payload,
            dedupe_key=dedupe_key,
            settings_version=settings_version,
            max_attempts=max_attempts,
        )
        try:
            with self.session.begin_nested():
                self.session.add(task)
                self.session.flush()
        except IntegrityError:
            if dedupe_key:
                # SAVEPOINT 已回滚唯一冲突，外层事务仍可继续写入批次关系。
                existing = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))))
                if existing:
                    return existing
            raise DatabaseError("task_submit_conflict")
        return task

    def _lock_lane(self, lane: str) -> None:
        """在当前事务中锁定整个 lane，保证跨进程槽位和背压判断原子化。"""
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"mememeow:lane:{lane}"))))

    def _ensure_lane_slots(self, lane: str, capacity: int) -> None:
        """幂等创建 lane 槽位；调用者必须先持有 lane advisory lock。"""
        for number in range(max(1, int(capacity))):
            if self.session.get(TaskLaneSlot, (lane, number)) is None:
                self.session.add(TaskLaneSlot(lane=lane, slot_number=number))
        self.session.flush()

    def _release_lane_slot(self, task_scope_id: str, task_id: str, *, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """释放任务占用的数据库槽位；owner/generation 用于旧 Worker fencing。"""
        statement = select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == task_scope_id, TaskLaneSlot.task_id == task_id).with_for_update()
        slot = self.session.scalar(statement)
        if slot is None:
            return False
        if owner is not None and slot.lease_owner not in {None, owner}:
            return False
        if claim_generation is not None and slot.claim_generation not in {None, claim_generation}:
            return False
        slot.task_scope_id = None
        slot.task_id = None
        slot.lease_owner = None
        slot.claim_generation = None
        slot.lease_expires_at = None
        return True

    def slot_for_task(self, task_id: str) -> TaskLaneSlot | None:
        """读取当前 scope 任务占用的槽位，用于安全摘要和诊断。"""
        return self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id))

    def recover_expired(self, *, owner: str, limit: int = 1000, exclude_task_types: set[str] | frozenset[str] | None = None, include_task_types: set[str] | frozenset[str] | None = None) -> list[str]:
        """恢复失效租约；可按任务类型限制专用 Worker 的恢复范围。"""
        now = utcnow()
        filters = [Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now]
        if exclude_task_types:
            filters.append(~Task.task_type.in_(exclude_task_types))
        if include_task_types:
            filters.append(Task.task_type.in_(include_task_types))
        rows = list(
            self.session.scalars(
                select(Task)
                .where(*filters)
                .order_by(Task.lease_expires_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(max(1, min(int(limit), 5000)))
            )
        )
        queued: list[str] = []
        for task in rows:
            previous_owner = task.lease_owner
            previous_generation = task.claim_generation
            recovery_error = {"error": "lease_expired", "message": "Worker 租约已过期"}
            append_task_error_history(
                task,
                recovery_error,
                attempt=task.attempt_count,
                executor_attempt_id=task.executor_attempt_id,
                session_id=task.resume_session_id,
                occurred_at=now.isoformat(),
            )
            if task.attempt_count < task.max_attempts:
                task.status = "queued"
                task.available_at = now
                task.message = "租约已过期，等待重新认领"
                task.error = recovery_error
                queued.append(task.id)
            else:
                task.status = "failed"
                task.completed_at = now
                task.message = "任务达到最大尝试次数"
                terminal_error = {"error": "max_attempts_exceeded", "message": "任务达到最大尝试次数"}
                append_task_error_history(
                    task,
                    terminal_error,
                    attempt=task.attempt_count,
                    executor_attempt_id=task.executor_attempt_id,
                    session_id=task.resume_session_id,
                    occurred_at=now.isoformat(),
                )
                task.error = terminal_error
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=previous_owner, claim_generation=previous_generation)
        self.session.flush()
        return queued

    def interrupt_owner(self, owner: str) -> int:
        """将当前 Worker 无法继续管理的 running 任务标记为可诊断失败。"""
        now = utcnow()
        rows = list(self.session.scalars(select(Task).where(Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_owner == owner).with_for_update(skip_locked=True)))
        for task in rows:
            interrupted_error = {"error": "task_interrupted", "message": "任务执行 Worker 已停止"}
            append_task_error_history(
                task,
                interrupted_error,
                attempt=task.attempt_count,
                executor_attempt_id=task.executor_attempt_id,
                session_id=task.resume_session_id,
                occurred_at=now.isoformat(),
            )
            task.status = "failed"
            task.completed_at = now
            task.message = "Worker 已停止"
            task.error = interrupted_error
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner)
        self.session.flush()
        return len(rows)

    def cancel(self, task_id: str, *, error: dict[str, Any], message: str) -> bool:
        """取消当前 scope 的 queued/running 任务并释放其 lane 槽位。"""
        task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
        if task is None or task.status in ("succeeded", "failed"):
            return False
        task.status = "failed"
        task.completed_at = utcnow()
        task.message = message
        safe_error = append_task_error_history(
            task,
            error,
            attempt=task.attempt_count,
            executor_attempt_id=task.executor_attempt_id,
            session_id=task.resume_session_id,
            occurred_at=utcnow().isoformat(),
        )
        task.error = safe_error
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = utcnow()
        self._release_lane_slot(task.scope_id, task.id, owner=None)
        self.session.flush()
        return True

    def _claim_lane_slot(self, task: Task, *, owner: str, lease_expires_at: datetime, capacity: int) -> bool:
        """为任务原子分配可用槽位，过期槽位可被新 claim 回收。"""
        self._lock_lane(task.lane)
        self._ensure_lane_slots(task.lane, capacity)
        now = utcnow()
        candidate = self.session.scalar(
            select(TaskLaneSlot)
            .where(
                TaskLaneSlot.lane == task.lane,
                TaskLaneSlot.slot_number < max(1, int(capacity)),
                (
                    (TaskLaneSlot.task_id.is_(None))
                    | (TaskLaneSlot.lease_expires_at.is_(None))
                    | (TaskLaneSlot.lease_expires_at <= now)
                ),
            )
            .order_by(TaskLaneSlot.slot_number)
            .limit(1)
        )
        if candidate is None:
            return False
        holder = None
        if candidate.task_id is not None:
            # 有效 Task 租约与已过期 slot 不一致时不能覆盖旧执行者；否则一个
            # slot 可能同时被两个 Worker 视为可用，破坏 lane fencing。先锁
            # holder，再锁 slot，与 heartbeat/fenced 写回保持 Task -> slot 顺序。
            holder = self.session.scalar(
                select(Task)
                .where(Task.scope_id == candidate.task_scope_id, Task.id == candidate.task_id)
                .with_for_update()
            )
        slot = self.session.scalar(
            select(TaskLaneSlot)
            .where(
                TaskLaneSlot.lane == task.lane,
                TaskLaneSlot.slot_number == candidate.slot_number,
            )
            .with_for_update(skip_locked=True)
        )
        if slot is None:
            return False
        if slot.task_id != candidate.task_id or slot.task_scope_id != candidate.task_scope_id:
            return False
        if slot.task_id is not None and (slot.lease_expires_at is not None and slot.lease_expires_at > now):
            return False
        if holder is not None and holder.status == "running":
            if holder.lease_expires_at is None or holder.lease_expires_at > now:
                raise DatabaseError("agent_lane_slot_inconsistent")
        slot.task_scope_id = task.scope_id
        slot.task_id = task.id
        slot.lease_owner = owner
        slot.claim_generation = None
        slot.lease_expires_at = lease_expires_at
        self.session.flush()
        return True

    def _recover_expired_lane_locked(
        self,
        *,
        lane: str,
        now: datetime,
        limit: int = 5000,
        scope_id: str | None = None,
        exclude_task_types: set[str] | frozenset[str] | None = None,
    ) -> list[str]:
        """在已持有 lane advisory lock 时恢复该 lane 的过期任务。

        公平 claim、租约恢复和槽位释放统一使用 lane -> Task -> slot 的锁顺序，
        避免恢复线程与调度线程交叉持锁造成死锁。
        """
        filters = [Task.lane == lane, Task.status == "running", Task.lease_expires_at < now]
        if scope_id is not None:
            filters.append(Task.scope_id == scope_id)
        if exclude_task_types:
            filters.append(~Task.task_type.in_(exclude_task_types))
        rows = list(
            self.session.scalars(
                select(Task)
                .where(*filters)
                .order_by(Task.lease_expires_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(max(1, min(int(limit), 5000)))
            )
        )
        queued: list[str] = []
        for task in rows:
            previous_owner = task.lease_owner
            previous_generation = task.claim_generation
            recovery_error = {"error": "lease_expired", "message": "Worker 租约已过期"}
            append_task_error_history(
                task,
                recovery_error,
                attempt=task.attempt_count,
                executor_attempt_id=task.executor_attempt_id,
                session_id=task.resume_session_id,
                occurred_at=now.isoformat(),
            )
            if task.attempt_count < task.max_attempts:
                task.status = "queued"
                task.available_at = now
                task.message = "租约已过期，等待重新认领"
                task.error = recovery_error
                queued.append(task.id)
            else:
                task.status = "failed"
                task.completed_at = now
                task.message = "任务达到最大尝试次数"
                terminal_error = {"error": "max_attempts_exceeded", "message": "任务达到最大尝试次数"}
                append_task_error_history(
                    task,
                    terminal_error,
                    attempt=task.attempt_count,
                    executor_attempt_id=task.executor_attempt_id,
                    session_id=task.resume_session_id,
                    occurred_at=now.isoformat(),
                )
                task.error = terminal_error
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=previous_owner, claim_generation=previous_generation)
        self.session.flush()
        return queued

    def _clear_expired_lane_slots_locked(self, *, lane: str, now: datetime, capacity: int) -> None:
        """清理孤儿或已失效的 lane slot，并拒绝有效租约不一致状态。"""
        # 不要先锁 slot 再锁 holder Task：heartbeat/fenced 写回遵守 Task ->
        # slot 顺序，反向顺序会在租约刚过期的边界形成死锁。slot 先以快照读
        # 出来，随后按 Task -> slot 重新读取并复核，lane advisory lock 已经
        # 串行化其它 claim；非 claim 的 heartbeat 则不会与本路径交叉持锁。
        candidates = list(
            self.session.scalars(
                select(TaskLaneSlot)
                .where(
                    TaskLaneSlot.lane == lane,
                    TaskLaneSlot.slot_number < max(1, int(capacity)),
                    TaskLaneSlot.task_id.is_not(None),
                    (
                        TaskLaneSlot.lease_expires_at.is_(None)
                        | (TaskLaneSlot.lease_expires_at <= now)
                    ),
                )
            )
        )
        for candidate in candidates:
            holder = self.session.scalar(
                select(Task)
                .where(Task.scope_id == candidate.task_scope_id, Task.id == candidate.task_id)
                .with_for_update()
            )
            slot = self.session.scalar(
                select(TaskLaneSlot)
                .where(
                    TaskLaneSlot.lane == lane,
                    TaskLaneSlot.slot_number == candidate.slot_number,
                )
                .with_for_update()
            )
            if slot is None or slot.task_id != candidate.task_id or slot.task_scope_id != candidate.task_scope_id:
                continue
            if slot.lease_expires_at is None or slot.lease_expires_at > now:
                continue
            if holder is not None and holder.status == "running":
                if holder.lease_expires_at is None or holder.lease_expires_at > now:
                    raise DatabaseError("agent_lane_slot_inconsistent")
            slot.task_scope_id = None
            slot.task_id = None
            slot.lease_owner = None
            slot.claim_generation = None
            slot.lease_expires_at = None
        self.session.flush()

    def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int = 120,
        lane: str = "agent",
        lane_capacity: int = 1,
        scope_capacity: int | None = None,
        scope_id: str | None = None,
        exclude_task_types: set[str] | frozenset[str] | None = None,
        exclude_image_pipeline: bool = False,
    ) -> Task | None:
        """在 lane 事务内按最久未服务 scope 公平认领一个任务。

        ``scope_id`` 只供内部恢复或测试缩小候选范围；正常 Agent manager 必须
        省略它，让候选 scope 完全来自持久 Task.scope_id。客户端 payload、user_id
        和进程内 cursor 不参与选择。返回的 Task.scope_id 是后续 facade 装配的
        唯一可信归属；公平状态、slot、claim generation 和 lease 会在本事务一起
        提交，任一步失败都会由 UnitOfWork 回滚。
        """
        if not isinstance(owner, str) or not owner.strip():
            raise DatabaseError("agent_claim_owner_invalid")
        if not isinstance(lane, str) or not lane.strip() or len(lane) > 64:
            raise DatabaseError("agent_claim_lane_invalid")
        try:
            capacity, limit = _validate_lane_capacities(lane_capacity, scope_capacity)
            assert capacity is not None
            lease_seconds = max(1, min(int(lease_seconds), 86400))
        except (TypeError, ValueError, AssertionError) as exc:
            raise DatabaseError("agent_claim_config_invalid") from exc
        if scope_id is not None:
            try:
                scope_id = ScopeContext(scope_id).scope_id
            except (TypeError, ValueError) as exc:
                raise DatabaseError("task_scope_invalid") from exc
        now = utcnow()
        try:
            # 所有跨 scope 的读写都必须在同一 lane advisory lock 内完成。
            self._lock_lane(lane)
            self._ensure_lane_slots(lane, capacity)
            self._recover_expired_lane_locked(lane=lane, now=now, scope_id=scope_id, exclude_task_types=exclude_task_types)
            self._clear_expired_lane_slots_locked(lane=lane, now=now, capacity=capacity)

            candidate_filters = [Task.lane == lane, Task.status == "queued", Task.available_at <= now]
            if scope_id is not None:
                candidate_filters.append(Task.scope_id == scope_id)
            if exclude_task_types:
                candidate_filters.append(~Task.task_type.in_(exclude_task_types))
            if exclude_image_pipeline:
                candidate_filters.append(
                    ~(
                        Task.task_type.in_(IMAGE_PROCESSING_LANE_TYPES)
                        & Task.submission_mode.in_(('pipeline', 'standalone'))
                    )
                )
            candidate_scope_ids = list(self.session.scalars(select(Task.scope_id).where(*candidate_filters).distinct()))
            if not candidate_scope_ids:
                return None

            # 公平行按 lane/scope 唯一键惰性建立，初次平局交给 Scope.created_at 和
            # scope ID，动态加入的 scope 不依赖任何进程内状态获得确定顺序。
            for candidate_scope in candidate_scope_ids:
                rows = list(
                    self.session.scalars(
                        select(TaskLaneFairness)
                        .where(TaskLaneFairness.lane == lane, TaskLaneFairness.scope_id == candidate_scope)
                        .with_for_update()
                    )
                )
                if len(rows) > 1:
                    raise DatabaseError("agent_fairness_unavailable")
                if not rows:
                    self.session.add(TaskLaneFairness(lane=lane, scope_id=candidate_scope, last_dispatch_sequence=0))
            self.session.flush()
            fairness_rows = list(
                self.session.scalars(
                    select(TaskLaneFairness).where(TaskLaneFairness.lane == lane).with_for_update()
                )
            )
            if any(not isinstance(row.last_dispatch_sequence, int) or row.last_dispatch_sequence < 0 for row in fairness_rows):
                raise DatabaseError("agent_fairness_unavailable")
            fairness_by_scope: dict[str, TaskLaneFairness] = {}
            for row in fairness_rows:
                if row.scope_id in fairness_by_scope:
                    raise DatabaseError("agent_fairness_unavailable")
                fairness_by_scope[row.scope_id] = row
            if any(candidate not in fairness_by_scope for candidate in candidate_scope_ids):
                raise DatabaseError("agent_fairness_unavailable")
            scope_rows = {
                row.id: row
                for row in self.session.scalars(select(Scope).where(Scope.id.in_(candidate_scope_ids)))
            }
            if len(scope_rows) != len(set(candidate_scope_ids)):
                raise DatabaseError("agent_fairness_unavailable")
            ordered_scopes = sorted(
                candidate_scope_ids,
                key=lambda candidate: (
                    fairness_by_scope[candidate].last_dispatch_sequence,
                    scope_rows[candidate].created_at,
                    candidate,
                ),
            )
            next_sequence = max((row.last_dispatch_sequence for row in fairness_rows), default=0) + 1
            for candidate_scope in ordered_scopes:
                running = self.session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.scope_id == candidate_scope,
                        Task.lane == lane,
                        Task.status == "running",
                        Task.lease_expires_at > now,
                    )
                ) or 0
                if int(running) >= limit:
                    continue
                task_filters = [
                    Task.scope_id == candidate_scope,
                    Task.lane == lane,
                    Task.status == "queued",
                    Task.available_at <= now,
                ]
                if exclude_task_types:
                    task_filters.append(~Task.task_type.in_(exclude_task_types))
                if exclude_image_pipeline:
                    task_filters.append(
                        ~(
                            Task.task_type.in_(IMAGE_PROCESSING_LANE_TYPES)
                            & Task.submission_mode.in_(('pipeline', 'standalone'))
                        )
                    )
                task = self.session.scalar(
                    select(Task)
                    .where(*task_filters)
                    .order_by(Task.available_at, Task.created_at, Task.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if task is None:
                    continue
                expires_at = now + timedelta(seconds=lease_seconds)
                if not self._claim_lane_slot(task, owner=owner, lease_expires_at=expires_at, capacity=capacity):
                    # lane 已由本事务独占；无 slot 意味着全局背压，公平序号不得推进。
                    return None
                task.status = "running"
                task.claim_generation += 1
                task.attempt_count += 1
                task.lease_owner = owner
                task.lease_expires_at = expires_at
                task.started_at = task.started_at or now
                task.updated_at = now
                slot = self.session.scalar(
                    select(TaskLaneSlot)
                    .where(TaskLaneSlot.task_scope_id == task.scope_id, TaskLaneSlot.task_id == task.id)
                    .with_for_update()
                )
                if slot is None:
                    raise DatabaseError("agent_lane_slot_inconsistent")
                slot.claim_generation = task.claim_generation
                fairness = fairness_by_scope[candidate_scope]
                fairness.last_dispatch_sequence = next_sequence
                fairness.updated_at = now
                self.session.flush()
                return task
            return None
        except DatabaseError:
            raise
        except SQLAlchemyError as exc:
            # 公平状态不可读、迁移缺失、唯一键损坏或事务无法提交时严禁回退
            # 到旧的竞争式 claim；调用方由稳定错误决定稍后重试/报警。
            raise DatabaseError("agent_fairness_unavailable") from exc

    def ensure_batch(self, batch_id: str) -> TaskBatch:
        """幂等创建当前 scope 的批次记录。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None:
            batch = TaskBatch(scope_id=self.scope.scope_id, batch_id=batch_id)
            self.session.add(batch)
            self.session.flush()
        return batch

    def add_batch_item(self, batch_id: str, task_id: str) -> None:
        """幂等写入 scope-safe 批次成员关系。"""
        batch = self.ensure_batch(batch_id)
        if batch.sealed:
            # 视觉任务重试或其事务内创建的 Agent 子任务可能晚于批次封口；只允许
            # 这两种阶段关系加入，其他外部任务继续拒绝，避免 finalizer 观察到错误成员。
            child = self.get(task_id)
            if child is None or child.task_type not in {"visual_embedding_generation", "meme_context_generation"}:
                raise DatabaseError("batch_sealed")
            # 视觉重试可能在旧 finalizer 完成后补入 Agent；重新打开收束状态，
            # 让该子任务终态后再次提交文本索引，而不是沿用旧批次快照。
            if batch.finalizer_state == "complete":
                batch.finalizer_state = "pending"
                batch.finalized_at = None
        if self.session.scalar(select(TaskBatchItem).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id, TaskBatchItem.task_id == task_id)) is None:
            self.session.add(TaskBatchItem(scope_id=self.scope.scope_id, batch_id=batch_id, task_id=task_id))
            self.session.flush()

    def context_task_for_target(self, meme_id: UUID | str, image_sha256: str) -> Task | None:
        """读取当前 scope、图片版本对应的最近 Agent 任务，供视觉完成幂等衔接使用。"""
        dedupe_key = f"context:{meme_id}:{image_sha256}"
        return self.session.scalar(
            select(Task)
            .where(
                Task.scope_id == self.scope.scope_id,
                Task.task_type == "meme_context_generation",
                (Task.dedupe_key == dedupe_key) | Task.dedupe_key.like(f"{dedupe_key}:%"),
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
        )

    def batch_ids_for_task(self, task_id: str) -> list[str]:
        """读取当前 scope 中与任务关联的所有批次，兼容任务去重后的复用路径。"""
        return list(self.session.scalars(select(TaskBatchItem.batch_id).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.task_id == task_id)))

    def finalize_batch(self, batch_id: str) -> bool:
        """在锁定已封口批次后检查成员终态，并原子写入索引刷新任务。"""
        return self._finalize_batch_task(batch_id) is not None

    def seal_batch(self, batch_id: str) -> None:
        """标记批次不再接收成员，防止 finalizer 观察到不完整成员集合。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None:
            raise DatabaseError("batch_not_found")
        batch.sealed = True
        self.session.flush()

    def finalize_batch_with_task(self, batch_id: str, *, task_type: str, payload: dict[str, Any], dedupe_key: str, settings_version: str | None = None, max_attempts: int = 3) -> Task | None:
        """锁定已封口批次，并在同一事务插入或复用唯一索引任务。"""
        return self._finalize_batch_task(batch_id, task_type=task_type, payload=payload, dedupe_key=dedupe_key, settings_version=settings_version, max_attempts=max_attempts)

    def pending_finalizer_batches(self, *, limit: int = 1000) -> list[str]:
        """列出当前 scope 中需要重试收束的已封口批次，供 Worker 启动恢复。"""
        statement = (
            select(TaskBatch.batch_id)
            .where(
                TaskBatch.scope_id == self.scope.scope_id,
                TaskBatch.sealed.is_(True),
                TaskBatch.finalizer_state.in_(('pending', 'submitted')),
            )
            .order_by(TaskBatch.created_at, TaskBatch.batch_id)
            .limit(max(1, min(int(limit), 5000)))
        )
        return list(self.session.scalars(statement))

    def _finalize_batch_task(self, batch_id: str, *, task_type: str | None = None, payload: dict[str, Any] | None = None, dedupe_key: str | None = None, settings_version: str | None = None, max_attempts: int = 3) -> Task | None:
        """在同一事务锁定批次并可选插入唯一索引任务。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None or not batch.sealed or batch.finalizer_state not in {"pending", "submitted"}:
            return None
        item_count = self.session.scalar(select(func.count()).select_from(TaskBatchItem).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id)) or 0
        if not item_count:
            return None
        active = self.session.scalar(select(func.count()).select_from(TaskBatchItem).join(Task, (Task.scope_id == TaskBatchItem.scope_id) & (Task.id == TaskBatchItem.task_id)).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id, ~Task.status.in_(("succeeded", "failed")))) or 0
        if active:
            return None
        task: Task | None = None
        if task_type is not None:
            if dedupe_key:
                task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))).with_for_update())
            if task is None:
                task = Task(id=uuid.uuid4().hex, scope_id=self.scope.scope_id, task_type=task_type, lane="default", payload=dict(payload or {}), dedupe_key=dedupe_key, settings_version=settings_version, max_attempts=max_attempts)
                self.session.add(task)
                try:
                    with self.session.begin_nested():
                        self.session.flush()
                except IntegrityError:
                    task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))))
                    if task is None:
                        raise DatabaseError("task_submit_conflict")
            batch.finalizer_state = "complete"
        else:
            batch.finalizer_state = "submitted"
        batch.finalized_at = utcnow()
        self.session.flush()
        return task

    def claim(self, *, owner: str, lease_seconds: int = 120, lane: str | None = None, task_id: str | None = None, lane_capacity: int | None = None, scope_capacity: int | None = None, exclude_task_types: set[str] | frozenset[str] | None = None) -> Task | None:
        """兼容 scope-bound 认领并递增 claim generation。

        Agent 正常调度必须使用 ``claim_next``；这个入口只保留给已由专用控制面
        选定的任务、租约恢复和历史兼容路径。lane 任务仍持有 advisory lock，
        因而不能绕过全局 slot 或可选 scope 上限。
        """
        now = utcnow()
        if lane and lane_capacity is not None:
            lane_capacity, scope_capacity = _validate_lane_capacities(lane_capacity, scope_capacity)
            assert lane_capacity is not None
            self._lock_lane(lane)
            self._ensure_lane_slots(lane, lane_capacity)
            self._recover_expired_lane_locked(lane=lane, now=now, scope_id=self.scope.scope_id, exclude_task_types=exclude_task_types)
            self._clear_expired_lane_slots_locked(lane=lane, now=now, capacity=lane_capacity)
            if scope_capacity is not None:
                running = self.session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.scope_id == self.scope.scope_id,
                        Task.lane == lane,
                        Task.status == "running",
                        Task.lease_expires_at > now,
                    )
                ) or 0
                if int(running) >= max(1, int(scope_capacity)):
                    return None
        else:
            self.recover_expired(owner=owner, exclude_task_types=exclude_task_types)
        filters = [Task.scope_id == self.scope.scope_id, Task.status == "queued", Task.available_at <= now]
        if lane:
            filters.append(Task.lane == lane)
        if task_id:
            filters.append(Task.id == task_id)
        if exclude_task_types:
            filters.append(~Task.task_type.in_(exclude_task_types))
        statement = select(Task).where(*filters).order_by(Task.available_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
        task = self.session.scalar(statement)
        if task is None:
            filters = [Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now, Task.attempt_count < Task.max_attempts]
            if lane:
                filters.append(Task.lane == lane)
            if task_id:
                filters.append(Task.id == task_id)
            if exclude_task_types:
                filters.append(~Task.task_type.in_(exclude_task_types))
            statement = select(Task).where(*filters).order_by(Task.lease_expires_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
            task = self.session.scalar(statement)
        if task is None:
            return None
        if lane and lane_capacity is not None:
            # task_id 过滤只缩小候选范围，不能绕过全局 lane 槽位上限。
            expires_at = now + timedelta(seconds=lease_seconds)
            if not self._claim_lane_slot(task, owner=owner, lease_expires_at=expires_at, capacity=lane_capacity):
                return None
        else:
            expires_at = now + timedelta(seconds=lease_seconds)
        task.status = "running"
        task.claim_generation += 1
        task.attempt_count += 1
        task.lease_owner = owner
        task.lease_expires_at = expires_at
        task.started_at = task.started_at or now
        task.updated_at = now
        if lane and lane_capacity:
            slot = self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == task.scope_id, TaskLaneSlot.task_id == task.id).with_for_update())
            if slot is not None and slot.lease_owner == owner:
                slot.claim_generation = task.claim_generation
        self.session.flush()
        return task

    def heartbeat(self, task_id: str, claim_generation: int, owner: str, lease_seconds: int = 120) -> bool:
        """在当前 claim 仍有效时续租，防止长时间外部调用被误判为崩溃。"""
        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        result = self.session.execute(update(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id, Task.claim_generation == claim_generation, Task.lease_owner == owner, Task.status == "running", Task.lease_expires_at > now).values(lease_expires_at=expires_at, updated_at=now))
        if result.rowcount != 1:
            return False
        slot = self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id).with_for_update())
        if slot is not None and slot.lease_owner == owner and slot.claim_generation in {None, claim_generation}:
            slot.lease_expires_at = expires_at
            self.session.flush()
        return True

    def fail_fenced(self, task_id: str, claim_generation: int, owner: str, *, error: dict[str, Any], message: str, retry: bool = True, result: Any | None = None, retry_delay_seconds: int = 0, resume_available: bool | None = None, resume_reason: str | None = None, session_id: str | None = None, executor_attempt_id: str | None = None) -> tuple[bool, bool]:
        """在 fencing 条件下失败或重新排队任务，并追加有限错误历史。"""
        now = utcnow()
        task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
        if task is None or task.status != "running" or task.claim_generation != claim_generation or task.lease_owner != owner or not task.lease_expires_at or task.lease_expires_at <= now:
            return False, False
        safe_error = sanitize_error(error)
        task.first_error = sanitize_error(task.first_error) if isinstance(task.first_error, dict) else safe_error
        task.error_history = append_error_history(
            task.error_history,
            safe_error,
            attempt=task.attempt_count,
            executor_attempt_id=executor_attempt_id or task.executor_attempt_id,
            session_id=session_id or task.resume_session_id,
            occurred_at=now.isoformat(),
        )
        if session_id:
            task.resume_session_id = session_id
        if executor_attempt_id:
            task.executor_attempt_id = executor_attempt_id
        if resume_available is not None:
            task.resume_available = bool(
                resume_available
                and normalize_identifier(task.resume_session_id, kind="session")
                and normalize_identifier(task.executor_attempt_id, kind="attempt")
            )
            task.resume_reason = resume_reason
            if task.resume_available and task.resume_started_at is None:
                task.resume_started_at = now
        should_retry = bool(retry and task.attempt_count < task.max_attempts)
        if should_retry:
            task.status = "queued"
            task.available_at = now + timedelta(seconds=max(0, min(int(retry_delay_seconds), 3600)))
            task.message = message
            task.error = safe_error
            if result is not None:
                task.result = result
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner, claim_generation=claim_generation)
        else:
            task.status = "failed"
            task.completed_at = now
            task.message = message
            task.error = safe_error
            if result is not None:
                task.result = result
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner, claim_generation=claim_generation)
        self.session.flush()
        return True, should_retry

    def update_fenced(self, task_id: str, claim_generation: int, owner: str, **changes: Any) -> bool:
        """仅在当前 claim 和租约有效时提交进度、终态或结果。"""
        now = utcnow()
        requested_status = changes.get("status")
        if requested_status not in {None, "succeeded", "failed"}:
            raise DatabaseError("invalid_task_transition")
        values = {key: value for key, value in changes.items() if hasattr(Task, key) and key != "status"}
        if requested_status in {"succeeded", "failed"}:
            values["status"] = requested_status
            values["completed_at"] = now
            values["lease_expires_at"] = None
            if requested_status == "succeeded":
                # 首次/历史错误由 first_error 和 error_history 保留；当前成功
                # 终态的主错误字段必须清空，避免 API 把成功显示为失败。
                values["error"] = None
                values["resume_available"] = False
                values["resume_reason"] = None
        values["updated_at"] = now
        result = self.session.execute(update(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id, Task.claim_generation == claim_generation, Task.lease_owner == owner, Task.status == "running", Task.lease_expires_at > now).values(**values))
        if result.rowcount != 1:
            return False
        if requested_status in {"succeeded", "failed"}:
            self._release_lane_slot(self.scope.scope_id, task_id, owner=owner, claim_generation=claim_generation)
            self.session.flush()
        return True

    def complete_fenced_with_provenance(self, task_id: str, claim_generation: int, owner: str, *, result: Any) -> bool:
        """原子提交图片 Agent 成功终态及反向图片 provenance。

        图片任务的审计结果和 Meme provenance 必须与 claim fencing 处于同一
        事务，否则查询方可能在任务已显示成功时读到尚未写入的 provenance。
        只有当前 owner、generation 和租约仍有效时才会修改两者。
        """
        now = utcnow()
        task = self.session.scalar(
            select(Task)
            .where(
                Task.scope_id == self.scope.scope_id,
                Task.id == task_id,
                Task.claim_generation == claim_generation,
                Task.lease_owner == owner,
                Task.status == "running",
                Task.lease_expires_at > now,
            )
            .with_for_update()
        )
        if task is None:
            return False
        task.status = "succeeded"
        task.progress = 1.0
        task.message = "任务完成"
        task.result = result
        task.error = None
        task.resume_available = False
        task.resume_reason = None
        task.completed_at = now
        task.updated_at = now
        task.lease_expires_at = None
        if task.task_type == "meme_context_generation":
            meme_id = (task.payload or {}).get("meme_id")
            if meme_id:
                try:
                    meme_id_value = UUID(str(meme_id))
                except (TypeError, ValueError):
                    meme_id_value = None
                if meme_id_value is not None:
                    try:
                        meme = self.session.scalar(
                            select(Meme)
                            .where(Meme.scope_id == self.scope.scope_id, Meme.id == meme_id_value)
                            .with_for_update()
                        )
                        if meme is not None:
                            # provenance 是可重建的附属事实；任务成功不能因为
                            # 历史用量行损坏而被回滚。
                            audit = ReverseImageUsageRepository(self.session, self.scope).aggregate_task(task_id)
                            provenance = dict(meme.provenance or {})
                            provenance["reverse_image"] = {
                                "policy": str((task.payload or {}).get("reverse_image_policy") or "forbid"),
                                **audit,
                            }
                            meme.provenance = provenance
                            meme.updated_at = now
                    except Exception:  # noqa: BLE001 - 附属写回失败不回滚任务终态
                        pass
        self._release_lane_slot(self.scope.scope_id, task_id, owner=owner, claim_generation=claim_generation)
        self.session.flush()
        return True

    def list(self, *, statuses: set[str] | None = None, task_types: set[str] | None = None, cursor: str | None = None, limit: int = 50) -> tuple[list[Task], str | None]:
        """按更新时间和 ID 稳定分页列出当前 scope 任务。"""
        statement = select(Task).where(Task.scope_id == self.scope.scope_id)
        if statuses:
            statement = statement.where(Task.status.in_(statuses))
        if task_types:
            statement = statement.where(Task.task_type.in_(task_types))
        if cursor:
            cursor_record = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == cursor))
            if cursor_record:
                statement = statement.where((Task.updated_at < cursor_record.updated_at) | ((Task.updated_at == cursor_record.updated_at) & (Task.id < cursor_record.id)))
        statement = statement.order_by(Task.updated_at.desc(), Task.id.desc()).limit(max(1, min(limit, 100)) + 1)
        records = list(self.session.scalars(statement))
        next_cursor = records[-1].id if len(records) > limit else None
        return records[:limit], next_cursor


class ReverseImageUsageRepository:
    """按 scope 读写反向图片用量事件并生成任务级审计摘要。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, request_id: str, *, for_update: bool = False) -> ReverseImageUsageEvent | None:
        """幂等读取当前 scope 的请求记录；全局 request_id 仍不会跨 scope 产生统计。"""
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_binding(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> ReverseImageUsageEvent | None:
        """按完整 callback 绑定读取旧 usage 事实，拒绝不确定的历史别名。

        callback 迁移只为 callback 表安装逻辑唯一约束；旧 usage 可能在进程崩溃窗口
        中先于 callback 行落盘。若完整绑定命中多条 usage，调用方不能猜测权威 ID，
        因此以稳定冲突错误停止恢复。
        """
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.task_id == task_id,
            ReverseImageUsageEvent.claim_generation == claim_generation,
            ReverseImageUsageEvent.attempt == attempt,
            ReverseImageUsageEvent.operation == operation,
            ReverseImageUsageEvent.target_sha256 == target_sha256,
            ReverseImageUsageEvent.input_digest == input_digest,
        ).order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
        if for_update:
            statement = statement.with_for_update()
        try:
            rows = list(self.session.scalars(statement))
        except SQLAlchemyError as exc:
            raise DatabaseError("usage_binding_unavailable") from exc
        if len(rows) > 1:
            raise DatabaseError("usage_request_conflict")
        return rows[0] if rows else None

    @staticmethod
    def _same_request(
        event: ReverseImageUsageEvent,
        *,
        task_id: str,
        cache_key: str,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """校验 request id 的完整执行绑定，防止旧 claim 借新输入复用事实。"""
        if event.task_id != task_id or event.cache_key != cache_key:
            raise DatabaseError("usage_request_conflict")
        expected = (claim_generation, attempt, operation, target_sha256, input_digest)
        actual = (event.claim_generation, event.attempt, event.operation, event.target_sha256, event.input_digest)
        # 旧事件的全空绑定仍可由旧兼容入口读取；一旦事件或本次请求带有
        # 执行绑定，就必须逐字段一致，不能用缺省字段绕过 request id 约束。
        if (any(value is not None for value in expected) or any(value is not None for value in actual)) and actual != expected:
            raise DatabaseError("usage_request_conflict")
        return event

    def create(
        self,
        *,
        request_id: str,
        task_id: str,
        meme_id: UUID | str | None,
        cache_key: str,
        cache_status: str,
        provider: str | None = None,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """创建一次逻辑检索事件；重复 request_id 返回现有事件以保持幂等。"""
        parsed_meme_id: UUID | None = None
        if meme_id:
            try:
                parsed_meme_id = UUID(str(meme_id))
            except (ValueError, TypeError) as exc:
                raise DatabaseError("meme_not_found") from exc
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        event = ReverseImageUsageEvent(
            request_id=request_id,
            scope_id=self.scope.scope_id,
            task_id=task_id,
            meme_id=parsed_meme_id,
            cache_key=cache_key,
            cache_status=cache_status,
            provider=provider,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            # 保存点只回滚这次幂等插入，不能撤销调用方 UOW 中已经完成的任务写入。
            with self.session.begin_nested():
                self.session.add(event)
                self.session.flush()
        except IntegrityError:
            existing = self.get(request_id, for_update=True)
            if existing is None:
                raise DatabaseError("usage_event_conflict")
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        return event

    def mark_provider_started(self, request_id: str) -> ReverseImageUsageEvent:
        """原子标记供应商逻辑调用已开始，计数只会从 false 变为 true 一次。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        if event.completed_at is not None:
            return event
        if not event.provider_called:
            event.provider_called = True
            event.provider_started_at = utcnow()
            event.outcome = "started"
            self.session.flush()
        return event

    def finish(
        self,
        request_id: str,
        *,
        cache_status: str | None = None,
        outcome: str,
        retryable: bool = False,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ReverseImageUsageEvent:
        """写入供应商或缓存终态；失败事件保留以供恢复和审计。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        # 已完成事件是幂等终态；重试不能用另一份响应覆盖原始审计事实。
        if event.completed_at is not None:
            return event
        if cache_status is not None:
            event.cache_status = cache_status
        event.outcome = outcome
        event.retryable = retryable
        event.result = result
        event.error = error
        event.completed_at = utcnow()
        self.session.flush()
        return event

    def aggregate_task(self, task_id: str) -> dict[str, Any]:
        """按当前 scope 聚合任务事件，返回不含图片身份和供应商秘密的摘要。"""
        rows = list(
            self.session.scalars(
                select(ReverseImageUsageEvent)
                .where(
                    ReverseImageUsageEvent.scope_id == self.scope.scope_id,
                    ReverseImageUsageEvent.task_id == task_id,
                )
                .order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
            )
        )
        completed = sorted(
            (row for row in rows if row.completed_at is not None),
            key=lambda row: (row.completed_at, row.created_at, row.id),
        )
        return {
            "attempted": bool(rows),
            "used": any(row.outcome in {"success", "empty"} and bool((row.result or {}).get("used", True)) for row in rows),
            "cache_hits": sum(1 for row in rows if row.cache_status == "hit"),
            "provider_calls": sum(1 for row in rows if row.provider_called),
            "outcome": completed[-1].outcome if completed else ("started" if rows else "not_requested"),
            "request_count": len(rows),
        }


def _validate_callback_binding_values(
    *,
    request_id: str | None,
    task_id: str,
    claim_generation: int,
    attempt: int,
    operation: str,
    target_sha256: str,
    input_digest: str,
) -> None:
    """在事实层拒绝缺失或不完整的 callback 绑定字段。

    API 层已经验证过 callback 输入，但内存夹具、迁移工具和其它宿主适配器也可能
    直接调用 repository；这些路径不能因为绕过 HTTP 校验而退回 request-ID-only
    语义。数据库约束负责并发唯一性，这里负责在进入索引前保持两种事实层一致。
    """
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for char in request_id)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > 255
        or isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or not isinstance(operation, str)
        or not operation
        or len(operation) > 128
        or not isinstance(target_sha256, str)
        or len(target_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in target_sha256)
        or not isinstance(input_digest, str)
        or len(input_digest) != 64
        or any(char not in "0123456789abcdef" for char in input_digest)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")


class AgentCallbackRequestRepository:
    """按 scope 保存 callback request 绑定，供只读和副作用 callback 复用。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def ensure_schema_ready(self) -> bool:
        """确认 callback 复合唯一索引和绑定字段已经安装。

        callback 是可选的内部路径，不能因为旧安装尚未迁移而阻断 local 直连；但一旦
        请求进入该 repository，缺失索引、可空事实或表结构异常必须 fail-closed。
        """
        try:
            bind = self.session.get_bind()
            if bind is None or bind.dialect.name != "postgresql":
                raise DatabaseError("callback_binding_schema_unavailable")
            required_columns = (
                "scope_id",
                "task_id",
                "claim_generation",
                "attempt",
                "operation",
                "target_sha256",
                "input_digest",
            )
            columns_sql = ", ".join(f"'{column}'" for column in required_columns)
            row = self.session.execute(
                text(
                    f"""
                    SELECT
                        EXISTS(
                            SELECT 1
                              FROM pg_indexes
                             WHERE schemaname = current_schema()
                               AND tablename = 'agent_callback_requests'
                               AND indexname = 'uq_agent_callback_requests_logical'
                        ),
                        (
                            SELECT count(*)
                              FROM information_schema.columns
                             WHERE table_schema = current_schema()
                               AND table_name = 'agent_callback_requests'
                               AND column_name IN ({columns_sql})
                               AND is_nullable = 'NO'
                        )
                    """
                )
            ).one()
        except (SQLAlchemyError, DatabaseError) as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError("callback_binding_schema_unavailable") from exc
        if not bool(row[0]) or int(row[1]) != len(required_columns):
            raise DatabaseError("callback_binding_schema_unavailable")
        return True

    # 启动检查和隐藏测试使用的显式命名，均保持同一事实来源。
    is_schema_ready = ensure_schema_ready

    def get(self, request_id: str, *, for_update: bool = False) -> AgentCallbackRequest | None:
        """读取当前 scope 的 request 事实，不让 request id 跨 scope 产生命中。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_logical(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> AgentCallbackRequest | None:
        """按完整执行绑定和规范化输入读取唯一权威 callback 事实。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.task_id == task_id,
            AgentCallbackRequest.claim_generation == claim_generation,
            AgentCallbackRequest.attempt == attempt,
            AgentCallbackRequest.operation == operation,
            AgentCallbackRequest.target_sha256 == target_sha256,
            AgentCallbackRequest.input_digest == input_digest,
        )
        if for_update:
            statement = statement.with_for_update()
        try:
            return self.session.scalar(statement)
        except SQLAlchemyError as exc:
            # 历史重复逻辑键在迁移前不能猜测哪一行权威，读取也必须停止。
            raise DatabaseError("callback_binding_schema_unavailable") from exc

    @staticmethod
    def _same(
        row: AgentCallbackRequest,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """比较 callback request 的完整可信绑定，拒绝改绑重放。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def create(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """创建或复用 callback 逻辑事实，request ID 仅作为兼容性提示。"""
        return self.resolve(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )

    def resolve(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """按 request ID 和完整逻辑键解析唯一权威 callback 事实。

        解析顺序先比较当前 scope 下的 request ID，再查逻辑唯一键，最后才插入。并发
        插入遇到复合唯一约束冲突时重新读取竞争者事实，绝不通过新 ID 生成第三行。
        """
        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        self.ensure_schema_ready()
        existing = self.get(request_id, for_update=True) if request_id is not None else None
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        authoritative = self.get_by_logical(
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
            for_update=True,
        )
        if authoritative is not None:
            return authoritative
        if request_id is None:
            from backend.callbacks import canonical_callback_request_id

            request_id = canonical_callback_request_id(input_digest)
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        row = AgentCallbackRequest(
            scope_id=self.scope.scope_id,
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            authoritative = self.get_by_logical(
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
                for_update=True,
            )
            if authoritative is not None:
                return authoritative
            existing = self.get(request_id, for_update=True)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            raise DatabaseError("callback_request_conflict") from exc
        return row

    def finish(
        self,
        request_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> AgentCallbackRequest:
        """以幂等终态保存 callback 结果，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        row = self.get(request_id, for_update=True)
        if row is None:
            raise DatabaseError("callback_request_not_found")
        if row.completed_at is not None:
            # ``unknown_execution`` 只表示外部调用当时无法确认；随后若持久 usage
            # 事实已经明确成功或失败，可以收束为对应终态，但普通重试不会重新触发
            # provider，因为调用方会先读取该终态。
            if row.state != "unknown_execution" or state not in {"completed", "failed"}:
                return row
        row.state = state
        row.result = result
        row.error = error
        row.completed_at = utcnow()
        row.updated_at = row.completed_at
        self.session.flush()
        return row


@dataclass(slots=True)
class InMemoryCallbackRequest:
    """单进程 callback 事实夹具，字段与 PostgreSQL 模型保持一致。"""

    scope_id: str
    request_id: str
    task_id: str
    claim_generation: int
    attempt: int
    operation: str
    target_sha256: str
    input_digest: str
    state: str = "started"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = None  # type: ignore[assignment]
    updated_at: datetime = None  # type: ignore[assignment]
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """为内存夹具补齐与 ORM 默认值等价的创建时间。"""
        now = utcnow()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at


class InMemoryAgentCallbackRequestRepository:
    """提供与 PostgreSQL 相同双索引语义的单进程 callback repository。

    该夹具用于快速单元测试；生产并发门禁仍由 PostgreSQL 复合唯一索引提供。锁内
    同时维护 request ID 和完整逻辑键，确保不同 ID 的首次竞态只能得到同一事实。
    """

    def __init__(self, scope: ScopeContext | str = SCOPE_LOCAL):
        self.scope = scope if isinstance(scope, ScopeContext) else ScopeContext(scope)
        self._by_id: dict[str, InMemoryCallbackRequest] = {}
        self._by_logical: dict[tuple[str, int, int, str, str, str], InMemoryCallbackRequest] = {}
        self._lock = RLock()

    @staticmethod
    def _logical_key(*, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> tuple[str, int, int, str, str, str]:
        """构造与数据库唯一约束字段顺序一致的内存索引键。"""
        return (task_id, claim_generation, attempt, operation, target_sha256, input_digest)

    @staticmethod
    def _same(row: InMemoryCallbackRequest, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """比较完整绑定，拒绝 request ID 改绑。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def get(self, request_id: str) -> InMemoryCallbackRequest | None:
        """读取当前 scope 的 request ID 事实。"""
        with self._lock:
            return self._by_id.get(request_id)

    def get_by_logical(self, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest | None:
        """按完整逻辑键读取权威事实。"""
        with self._lock:
            return self._by_logical.get(self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest))

    def resolve(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """按 ID、逻辑键和确定性 ID 的顺序解析或创建 callback 事实。"""
        from backend.callbacks import canonical_callback_request_id

        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        with self._lock:
            existing = self._by_id.get(request_id) if request_id is not None else None
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            key = self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            authoritative = self._by_logical.get(key)
            if authoritative is not None:
                return authoritative
            selected_id = request_id or canonical_callback_request_id(input_digest)
            existing = self._by_id.get(selected_id)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            row = InMemoryCallbackRequest(
                scope_id=self.scope.scope_id,
                request_id=selected_id,
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
            self._by_id[selected_id] = row
            self._by_logical[key] = row
            return row

    def create(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """兼容旧调用名，执行双索引 resolve。"""
        return self.resolve(request_id=request_id, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)

    def finish(self, request_id: str, *, state: str, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> InMemoryCallbackRequest:
        """幂等保存 callback 终态，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        with self._lock:
            row = self._by_id.get(request_id)
            if row is None:
                raise DatabaseError("callback_request_not_found")
            if row.completed_at is not None and (row.state != "unknown_execution" or state not in {"completed", "failed"}):
                return row
            row.state = state
            row.result = result
            row.error = error
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            return row


# 兼容不同测试和宿主命名，避免为 callback 事实引入第二套 repository。
InMemoryCallbackRequestRepository = InMemoryAgentCallbackRequestRepository


class BlobStore:
    """绑定 scope 的文件存储；local 使用现有图片根目录，其他 scope 独立命名空间。"""

    def __init__(self, *, root: Path, scope: ScopeContext, storage_namespace: UUID | None = None, local: bool = False):
        self.scope = scope
        try:
            base_root = validate_controlled_root(root, create=True, writable=True)
            candidate = base_root if local else base_root / "scopes" / str(storage_namespace or uuid.uuid4()) / "images"
            self.root = validate_controlled_root(candidate, create=True, writable=True)
        except StorageRootError as exc:
            raise DatabaseError(str(exc)) from exc
        self.staging_root = self.root / ".staging"
        self.quarantine_root = self.root / ".quarantine"
        try:
            self.staging_root = validate_controlled_root(self.staging_root, create=True, writable=True)
            self.quarantine_root = validate_controlled_root(self.quarantine_root, create=True, writable=True)
            # BlobStore 也可能被离线迁移工具直接构造，确保非 Compose 夹具仍遵守目录契约。
            os.chmod(self.root, 0o700)
            os.chmod(self.staging_root, 0o700)
            os.chmod(self.quarantine_root, 0o700)
        except (StorageRootError, OSError) as exc:
            raise DatabaseError("storage_root_permissions_invalid") from exc

    def resolve(self, storage_key: str, *, must_exist: bool = True) -> Path:
        """安全解析 scope 内相对 key，拒绝绝对路径、穿越和符号链接逃逸。"""
        if not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")) or "\x00" in storage_key:
            raise DatabaseError("invalid_storage_key")
        # 暂存和隔离对象只允许由内部恢复流程访问，不能成为公开 Meme 路径。
        if storage_key == ".staging" or storage_key == ".quarantine" or storage_key.startswith((".staging/", ".quarantine/")):
            raise DatabaseError("internal_storage_key")
        lexical = self.root / storage_key
        current = self.root
        for part in Path(storage_key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        candidate = lexical.resolve()
        # 解析后的不存在目标仍需允许后续上传检查；只有现存路径才要求最终路径在根目录内。
        if candidate == self.root or self.root not in candidate.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not candidate.is_file() or candidate.is_symlink()):
            raise DatabaseError("file_not_found")
        return candidate

    def relative(self, path: Path) -> str:
        """返回绑定根目录下的 POSIX storage_key。"""
        candidate = Path(path)
        if candidate.is_symlink():
            raise DatabaseError("symlink_forbidden")
        resolved = candidate.resolve()
        if resolved == self.root or self.root not in resolved.parents:
            raise DatabaseError("path_forbidden")
        return resolved.relative_to(self.root).as_posix()

    def _safe_child(self, base: Path, key: str, *, must_exist: bool = False) -> Path:
        """解析 staging/quarantine 子路径并拒绝符号链接与越界。"""
        if not isinstance(key, str) or not key or key.startswith(("/", "\\")) or "\x00" in key:
            raise DatabaseError("invalid_storage_key")
        lexical = base / key
        current = base
        for part in Path(key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        resolved = lexical.resolve()
        if resolved == base or base not in resolved.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not resolved.is_file() or resolved.is_symlink()):
            raise DatabaseError("file_not_found")
        return resolved

    def stage_bytes(self, content: bytes, *, token: UUID) -> str:
        """以独占创建和 fsync 将已验证字节写入受控暂存区。"""
        key = f"{token.hex}.part"
        target = self._safe_child(self.staging_root, key)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise DatabaseError("staging_conflict") from exc
        except OSError as exc:
            raise DatabaseError("staging_write_failed") from exc
        self._fsync_directory(self.staging_root)
        return f".staging/{key}"

    def _fsync_directory(self, directory: Path) -> None:
        """尽力持久化目录项，平台不支持时保留原子文件语义。"""
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _key_path(self, key: str, *, must_exist: bool = False) -> Path:
        """解析普通 storage key 或内部暂存/隔离 key。"""
        if key.startswith(".staging/"):
            return self._safe_child(self.staging_root, key[len(".staging/"):], must_exist=must_exist)
        if key.startswith(".quarantine/"):
            return self._safe_child(self.quarantine_root, key[len(".quarantine/"):], must_exist=must_exist)
        return self.resolve(key, must_exist=must_exist)

    def link_move(self, source_key: str, target_key: str) -> None:
        """以 link+unlink 实现同文件系统的原子不覆盖移动。"""
        source = self._key_path(source_key, must_exist=True)
        target = self._key_path(target_key, must_exist=False)
        if target.exists() or target.is_symlink():
            raise DatabaseError("target_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target, follow_symlinks=False)
            os.unlink(source)
            self._fsync_directory(target.parent)
            self._fsync_directory(source.parent)
        except FileExistsError as exc:
            raise DatabaseError("target_exists") from exc
        except OSError as exc:
            try:
                if target.exists() and not source.exists():
                    os.unlink(target)
            except OSError:
                pass
            raise DatabaseError("file_move_failed") from exc

    def quarantine(self, source_key: str, *, token: UUID) -> str:
        """将图片移动到不可见隔离区并返回隔离 storage key。"""
        target_key = f".quarantine/{token.hex}.blob"
        self.link_move(source_key, target_key)
        return target_key

    def unlink(self, key: str) -> None:
        """只删除受控普通文件，拒绝符号链接和越界路径。"""
        target = self._key_path(key, must_exist=True)
        if target.is_symlink():
            raise DatabaseError("symlink_forbidden")
        try:
            os.unlink(target)
            self._fsync_directory(target.parent)
        except OSError as exc:
            raise DatabaseError("file_delete_failed") from exc

    def exists_with_identity(self, key: str, *, sha256: str | None = None, size_bytes: int | None = None) -> bool:
        """检查对象存在、非符号链接，并可选复核大小和 SHA。"""
        try:
            path = self._key_path(key, must_exist=True)
            stat = path.stat()
            if size_bytes is not None and stat.st_size != size_bytes:
                return False
            if sha256 is not None:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != sha256:
                    return False
            return True
        except (DatabaseError, OSError):
            return False


class StorageCoordinator:
    """协调 PostgreSQL Meme 记录与 scope-bound 文件存储的可恢复操作。

    上传、重命名和删除都先写入 ``storage_operations``，再执行文件动作；恢复器依据
    记录中的指纹和状态矩阵继续提交、补偿或隔离异常组合，避免中间文件进入正常查询。
    """

    _ACTIVE = {"prepared", "file_applied"}
    _TRANSITIONS = {
        "prepared": {"file_applied", "compensated", "blocked"},
        "file_applied": {"completed", "compensated", "blocked"},
        "completed": set(),
        "compensated": set(),
        "blocked": set(),
    }

    def __init__(self, resources: "DatabaseResources", *, scope_id: str | ScopeContext = SCOPE_LOCAL):
        """创建绑定 scope 的协调器；local 默认仅保留给开源兼容夹具。"""
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(self.scope.scope_id)

    def _set_status(self, operation: StorageOperation, status: str, *, error: dict[str, Any] | None = None, session: Session | None = None) -> None:
        """执行 storage operation 合法状态转移并保存诊断信息。"""
        if status == operation.status:
            return
        if status not in self._TRANSITIONS.get(operation.status, set()):
            raise DatabaseError("invalid_storage_transition")
        operation.status = status
        operation.error = error
        operation.updated_at = utcnow()
        (session or self._session).flush()

    @staticmethod
    def _title_fingerprint(record: Meme) -> str:
        """按自动命名 handler 的规则计算当前语境标题指纹。"""
        context = record.meme_context if isinstance(record.meme_context, dict) else {}
        raw_title = context.get("title") if isinstance(context, dict) else None
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        return hashlib.sha256(title.encode("utf-8")).hexdigest()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """创建当前 scope 的短数据库事务，并在退出时提交或回滚。"""
        with self.resources.factory() as session:
            self._session = session
            try:
                with session.begin():
                    yield session
            finally:
                self._session = None

    def upload(self, content: bytes, *, target_key: str, extension: str, context: dict[str, Any], provenance: dict[str, Any], meme_id: UUID | None = None) -> Meme:
        """暂存上传字节、创建 pending Meme，并在文件落位后完成 durable operation。"""
        try:
            validate_business_storage_key(target_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        target = self.blob_store._key_path(target_key)
        if target.exists() or target.is_symlink():
            raise DatabaseError("target_exists")
        digest = hashlib.sha256(content).hexdigest()
        token = uuid.uuid4()
        staging_key = self.blob_store.stage_bytes(content, token=token)
        try:
            with self._transaction() as session:
                existing = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.storage_key == target_key).with_for_update())
                if existing is not None:
                    raise DatabaseError("target_exists")
                record = Meme(id=meme_id or uuid.uuid4(), scope_id=self.scope.scope_id, storage_key=target_key, extension=extension.lower(), size_bytes=len(content), sha256=digest, context_status="pending", meme_context=context, provenance=provenance, extensions={}, revision=1)
                session.add(record)
                session.flush()
                session.add(StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="upload", operation_token=token, target_key=target_key, staging_key=staging_key, after_sha256=digest, after_size=len(content), status="prepared"))
                session.flush()
            self.blob_store.link_move(staging_key, target_key)
            with self._transaction() as session:
                operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise DatabaseError("storage_operation_missing")
                self._session = session
                self._set_status(operation, "file_applied", session=session)
                self._set_status(operation, "completed", session=session)
            return record
        except Exception:
            # 暂存文件没有数据库引用时可以安全清理；已写入 operation 的异常留给恢复器。
            try:
                if self.blob_store.exists_with_identity(staging_key, sha256=digest, size_bytes=len(content)):
                    self.blob_store.unlink(staging_key)
            except DatabaseError:
                pass
            raise

    def rename(self, meme_id: UUID | str, *, target_key: str) -> Meme:
        """记录重命名意图、原子移动文件并提交同一 Meme 的新 storage_key。"""
        try:
            validate_business_storage_key(target_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        token = uuid.uuid4()
        with self._transaction() as session:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            if session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.storage_key == target_key, Meme.id != record.id)) is not None:
                raise DatabaseError("target_exists")
            target_path = self.blob_store._key_path(target_key)
            if target_path.exists() or target_path.is_symlink():
                raise DatabaseError("target_exists")
            operation = StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="rename", operation_token=token, source_key=record.storage_key, target_key=target_key, before_sha256=record.sha256, after_sha256=record.sha256, before_size=record.size_bytes, after_size=record.size_bytes, status="prepared")
            session.add(operation)
            session.flush()
        try:
            self.blob_store.link_move(record.storage_key, target_key)
        except Exception:
            raise
        with self._transaction() as session:
            operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.operation_token == token,
                )
                .with_for_update()
            )
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if operation is None or record is None:
                raise DatabaseError("storage_operation_missing")
            self._session = session
            self._set_status(operation, "file_applied", session=session)
            record.storage_key = target_key
            record.revision += 1
            record.updated_at = utcnow()
            self._set_status(operation, "completed", session=session)
            session.flush()
            return record

    def rename_if_current(
        self,
        meme_id: UUID | str,
        *,
        target_key: str,
        expected_source_key: str,
        expected_sha256: str,
        expected_revision: int,
        task_id: str,
        claim_generation: int,
        attempt: int,
        claim_owner: str,
        expected_title_fingerprint: str | None = None,
    ) -> Meme:
        """在任务 claim 与 Meme 事实仍匹配时执行一次 CAS 重命名。

        第一段事务锁定 Meme/Task 并记录 ``StorageOperation``，文件移动完成后第二段
        事务再次复核所有 fencing 输入。任一复核失败都会阻断操作恢复，避免未知文件
        副作用被当作普通命名警告；实际 claim owner 必须与当前 Task lease owner 完全
        一致，不能只依赖 generation 和 attempt。

        ``storage_key_changed`` 只表示同一 SHA 的 Meme 已经被人工改名，调用方可将其
        降级为 warning；SHA、revision、语境指纹、claim 或文件副作用无法确认时必须
        保持 blocked/unknown_execution。
        """
        try:
            validate_business_storage_key(target_key)
            validate_business_storage_key(expected_source_key)
        except ValueError as exc:
            raise DatabaseError("invalid_filename") from exc
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(claim_owner, str)
            or not claim_owner
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or not isinstance(claim_generation, int)
            or claim_generation < 1
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise DatabaseError("target_changed")

        def current_title_fingerprint(record: Meme) -> str:
            """从数据库中的当前标题计算与 handler 一致的输入指纹。"""
            return self._title_fingerprint(record)

        def mark_blocked(error: str) -> None:
            """在文件副作用已发生但 finalize 不确定时持久化 blocked。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    if operation is not None and operation.status in self._ACTIVE:
                        # finalize 失败时不再复用可能已抛错的状态转移 helper，直接
                        # 持久化 blocked 事实，确保恢复器不会把副作用当作可重放。
                        operation.status = "blocked"
                        operation.error = {"error": error}
                        operation.updated_at = utcnow()
                        session.flush()
            except Exception:  # noqa: BLE001 - 数据库本身不可用时保留原始异常
                return

        def compensate_manual_replacement() -> bool:
            """识别文件移动前同图手动改名，并安全结束未执行的操作。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    record = session.scalar(
                        select(Meme)
                        .where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id)))
                        .with_for_update()
                    )
                    if operation is None or operation.status != "prepared" or record is None:
                        return False
                    title_matches = expected_title_fingerprint is None or current_title_fingerprint(record) == expected_title_fingerprint
                    same_image = record.sha256.lower() == expected_sha256.lower()
                    current_is_target = (
                        record.storage_key == target_key
                        and record.revision == expected_revision + 1
                        and self.blob_store.exists_with_identity(target_key, sha256=expected_sha256, size_bytes=record.size_bytes)
                    )
                    current_is_replacement = record.storage_key != source_key and not current_is_target
                    target_path = self.blob_store._key_path(target_key, must_exist=False)
                    target_absent = not target_path.exists() and not target_path.is_symlink()
                    if not (same_image and title_matches and current_is_replacement and target_absent):
                        if not (same_image and title_matches and current_is_target):
                            return False
                    self._set_status(operation, "compensated", error={"error": "storage_key_changed"}, session=session)
                    return True
            except Exception:  # noqa: BLE001 - 无法确认时必须保留 unknown 语义
                return False

        def compensate_unapplied_target_conflict() -> bool:
            """在目标文件于预检后被占用时补偿尚未发生的文件动作。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    record = session.scalar(
                        select(Meme)
                        .where(
                            Meme.scope_id == self.scope.scope_id,
                            Meme.id == UUID(str(meme_id)),
                        )
                        .with_for_update()
                    )
                    if operation is None or operation.status != "prepared" or record is None:
                        return False
                    if (
                        record.storage_key != source_key
                        or record.revision != expected_revision
                        or record.sha256.lower() != expected_sha256.lower()
                        or (expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint)
                    ):
                        return False
                    source_ok = self.blob_store.exists_with_identity(
                        source_key,
                        sha256=expected_sha256,
                        size_bytes=record.size_bytes,
                    )
                    target_path = self.blob_store._key_path(target_key, must_exist=False)
                    if not source_ok or not target_path.exists() or target_path.is_symlink():
                        return False
                    self._set_status(operation, "compensated", error={"error": "target_exists"}, session=session)
                    return True
            except Exception:  # noqa: BLE001 - 无法证明未发生副作用时保留未知语义
                return False

        token = uuid.uuid4()
        source_key = expected_source_key
        expected_size: int | None = None
        try:
            with self._transaction() as session:
                record = session.scalar(
                    select(Meme).where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.id == UUID(str(meme_id)),
                    ).with_for_update()
                )
                task = session.scalar(
                    select(Task).where(
                        Task.scope_id == self.scope.scope_id,
                        Task.id == task_id,
                    ).with_for_update()
                )
                now = utcnow()
                if (
                    task is None
                    or task.task_type != "image_auto_rename"
                    or task.image_stage != "auto_rename"
                    or task.status != "running"
                    or task.claim_generation != claim_generation
                    or task.attempt_count != attempt
                    or task.lease_expires_at is None
                    or task.lease_expires_at <= now
                    or task.lease_owner != claim_owner
                ):
                    raise DatabaseError("claim_expired")
                if record is None or record.sha256.lower() != expected_sha256.lower():
                    raise DatabaseError("target_changed")
                if expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint:
                    raise DatabaseError("target_changed")
                if record.storage_key != expected_source_key:
                    raise DatabaseError("storage_key_changed")
                if record.revision != expected_revision:
                    raise DatabaseError("target_changed")
                # 同一 Meme 的既有存储操作可能仍有未确认副作用；即使本次派生结果
                # 与当前文件同名，也不能绕过 blocked/活动操作的 fail-closed 边界。
                unsettled_operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.meme_id == record.id,
                        StorageOperation.status.in_(("prepared", "file_applied", "blocked")),
                    )
                    .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.desc())
                    .with_for_update()
                )
                if unsettled_operation is not None:
                    raise DatabaseError("storage_operation_unknown")
                if target_key == record.storage_key:
                    # 目标名已经符合派生结果时仍需经过上面的 Task claim/CAS
                    # 校验和文件身份复核；文件被外部替换时不能把旧路径当作成功。
                    if not self.blob_store.exists_with_identity(
                        record.storage_key,
                        sha256=expected_sha256,
                        size_bytes=record.size_bytes,
                    ):
                        raise DatabaseError("target_changed")
                    # 复核通过后避免无意义地创建 storage operation。
                    return record
                if session.scalar(
                    select(Meme.id).where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.storage_key == target_key,
                        Meme.id != record.id,
                    )
                ) is not None:
                    raise DatabaseError("target_exists")
                target_path = self.blob_store._key_path(target_key)
                if target_path.exists() or target_path.is_symlink():
                    raise DatabaseError("target_exists")
                expected_size = record.size_bytes
                operation = StorageOperation(
                    scope_id=self.scope.scope_id,
                    meme_id=record.id,
                    operation_type="rename",
                    operation_token=token,
                    source_key=record.storage_key,
                    target_key=target_key,
                    before_sha256=record.sha256,
                    after_sha256=record.sha256,
                    before_size=record.size_bytes,
                    after_size=record.size_bytes,
                    expected_revision=expected_revision,
                    claim_generation=claim_generation,
                    attempt=attempt,
                    task_id=task_id,
                    expected_title_fingerprint=expected_title_fingerprint,
                    status="prepared",
                )
                session.add(operation)
                session.flush()
            # 数据库锁不能阻止外部进程替换文件；移动前复核源对象身份，避免把
            # 同名但不同字节的文件绑定到当前 Meme。
            if not self.blob_store.exists_with_identity(
                source_key,
                sha256=expected_sha256,
                size_bytes=expected_size,
            ):
                mark_blocked("source_identity_changed")
                raise DatabaseError("target_changed")
            try:
                self.blob_store.link_move(source_key, target_key)
            except (DatabaseError, OSError) as exc:
                if compensate_manual_replacement():
                    raise DatabaseError("storage_key_changed") from exc
                if isinstance(exc, DatabaseError) and exc.code == "target_exists" and compensate_unapplied_target_conflict():
                    raise DatabaseError("target_exists") from exc
                # 预检通过后文件动作仍可能在 link/unlink 边界失败；此时不能把
                # 未知副作用当作普通目标冲突，必须留下 blocked 事实交给恢复器。
                mark_blocked("rename_file_move_unknown")
                raise DatabaseError("storage_operation_unknown") from exc
            try:
                target_verified = self.blob_store.exists_with_identity(
                    target_key,
                    sha256=expected_sha256,
                    size_bytes=expected_size,
                )
                source_path = self.blob_store._key_path(source_key, must_exist=False)
                source_absent = not source_path.exists() and not source_path.is_symlink()
            except (DatabaseError, OSError) as exc:
                mark_blocked("rename_file_identity_unknown")
                raise DatabaseError("storage_operation_unknown") from exc
            if not target_verified or not source_absent:
                mark_blocked("rename_file_identity_mismatch")
                raise DatabaseError("storage_operation_unknown")
        except Exception:
            # ``prepared`` 操作必须交给恢复器判断；不要删除可能已完成的文件移动。
            raise

        blocked_error: str | None = None
        try:
            with self._transaction() as session:
                operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
                record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
                task = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
                now = utcnow()
                if operation is None or record is None:
                    blocked_error = "storage_operation_missing"
                elif (
                    task is None
                    or task.task_type != "image_auto_rename"
                    or task.image_stage != "auto_rename"
                    or task.status != "running"
                    or task.claim_generation != claim_generation
                    or task.attempt_count != attempt
                    or task.lease_expires_at is None
                    or task.lease_expires_at <= now
                    or task.lease_owner != claim_owner
                ):
                    blocked_error = "claim_expired"
                elif record.sha256.lower() != expected_sha256.lower():
                    blocked_error = "target_changed"
                elif expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint:
                    blocked_error = "target_changed"
                elif record.storage_key != expected_source_key:
                    blocked_error = "storage_key_changed" if record.sha256.lower() == expected_sha256.lower() else "target_changed"
                elif record.revision != expected_revision:
                    blocked_error = "target_changed"
                else:
                    target_verified = self.blob_store.exists_with_identity(
                        target_key,
                        sha256=expected_sha256,
                        size_bytes=expected_size,
                    )
                    source_path = self.blob_store._key_path(source_key, must_exist=False)
                    if not target_verified or source_path.exists() or source_path.is_symlink():
                        blocked_error = "rename_file_identity_mismatch"
                    else:
                        self._session = session
                        self._set_status(operation, "file_applied", session=session)
                        record.storage_key = target_key
                        record.revision += 1
                        record.updated_at = utcnow()
                        self._set_status(operation, "completed", session=session)
                        session.flush()
        except Exception as exc:  # noqa: BLE001 - 文件已移动，finalize 异常必须留痕
            mark_blocked("unknown_execution")
            raise DatabaseError("storage_operation_unknown") from exc
        if blocked_error is not None:
            # 文件已移动但数据库事实无法安全收束，operation 保持 blocked，由恢复/人工
            # 处置路径保留未知执行证据，调用方不能把它降级为 warning。
            mark_blocked(blocked_error)
            raise DatabaseError("storage_operation_unknown")
        with self.resources.factory() as session:
            return session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))))

    def delete(self, meme_id: UUID | str) -> None:
        """先将文件移入隔离区，再删除 Meme 记录并清理隔离对象。"""
        token = uuid.uuid4()
        with self._transaction() as session:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            operation = StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="delete", operation_token=token, source_key=record.storage_key, target_key=f".quarantine/{token.hex}.blob", before_sha256=record.sha256, before_size=record.size_bytes, status="prepared")
            session.add(operation)
            session.flush()
            source_key = record.storage_key
        self.blob_store.quarantine(source_key, token=token)
        with self._transaction() as session:
            operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.operation_token == token,
                )
                .with_for_update()
            )
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if operation is None or record is None:
                raise DatabaseError("storage_operation_missing")
            self._session = session
            self._set_status(operation, "file_applied", session=session)
            # 删除 Meme 前解除关联，保留 completed operation 作为恢复审计记录。
            operation.meme_id = None
            session.delete(record)
            session.flush()
            self._set_status(operation, "completed", session=session)
        try:
            self.blob_store.unlink(f".quarantine/{token.hex}.blob")
        except DatabaseError:
            # 清理失败不影响数据库权威删除，恢复扫描会继续处理隔离对象。
            pass

    def recover(self, *, limit: int = 100) -> dict[str, int]:
        """以 SKIP LOCKED 独占恢复未完成操作，并返回各状态处理计数。"""
        counts = {"completed": 0, "compensated": 0, "blocked": 0, "retried": 0}
        with self.resources.factory() as session:
            rows = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE))).order_by(StorageOperation.updated_at).with_for_update(skip_locked=True).limit(max(1, min(limit, 1000)))))
            for operation in rows:
                self._session = session
                try:
                    if operation.operation_type == "upload":
                        self._recover_upload(session, operation, counts)
                    elif operation.operation_type == "rename":
                        self._recover_rename(session, operation, counts)
                    elif operation.operation_type == "delete":
                        self._recover_delete(session, operation, counts)
                    else:
                        # 数据库 CHECK 已禁止新值，但旧安装或人工修复可能留下
                        # 未知类型；恢复器必须停在 blocked，不能静默丢掉副作用事实。
                        self._set_status(operation, "blocked", error={"error": "storage_operation_unknown_type"}, session=session)
                        counts["blocked"] += 1
                except DatabaseError as exc:
                    self._set_status(operation, "blocked", error={"error": exc.code, "message": str(exc)}, session=session)
                    counts["blocked"] += 1
            session.commit()
            self._session = None
        return counts

    def _recover_upload(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复上传暂存文件或补偿无文件的 Meme。"""
        assert operation.target_key and operation.staging_key
        target_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        stage_ok = self.blob_store.exists_with_identity(operation.staging_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        if operation.status == "prepared" and stage_ok and not target_ok:
            self.blob_store.link_move(operation.staging_key, operation.target_key)
            stage_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not stage_ok:
            self._set_status(operation, "file_applied", session=session)
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif not target_ok and not stage_ok:
            record = session.get(Meme, operation.meme_id) if operation.meme_id else None
            if record is not None:
                operation.meme_id = None
                self._session.delete(record)
            self._set_status(operation, "compensated", session=session)
            counts["compensated"] += 1
        else:
            raise DatabaseError("upload_recovery_ambiguous")

    def _recover_rename(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复重命名文件动作和数据库路径提交。"""
        assert operation.source_key and operation.target_key
        source_ok = self.blob_store.exists_with_identity(operation.source_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        target_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
        if record is None:
            raise DatabaseError("meme_not_found")
        if operation.task_id is not None:
            # 自动重命名的恢复必须仍属于创建 operation 时的叶子 claim。任务被
            # 重新认领、完成或租约过期后，不能让恢复器继续移动或 finalize 文件。
            task = session.scalar(
                select(Task)
                .where(Task.scope_id == self.scope.scope_id, Task.id == operation.task_id)
                .with_for_update()
            )
            now = utcnow()
            if (
                task is None
                or task.task_type != "image_auto_rename"
                or task.image_stage != "auto_rename"
                or task.status != "running"
                or operation.claim_generation is None
                or task.claim_generation != operation.claim_generation
                or operation.attempt is None
                or task.attempt_count != operation.attempt
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
            ):
                raise DatabaseError("rename_claim_expired")
        if (
            not isinstance(operation.before_sha256, str)
            or len(operation.before_sha256) != 64
            or not isinstance(operation.after_sha256, str)
            or len(operation.after_sha256) != 64
        ):
            raise DatabaseError("rename_operation_invalid")
        same_image = record.sha256.lower() == operation.before_sha256.lower()
        title_matches = operation.expected_title_fingerprint is None or self._title_fingerprint(record) == operation.expected_title_fingerprint
        already_finalized = (
            record.storage_key == operation.target_key
            and operation.expected_revision is not None
            and record.revision == operation.expected_revision + 1
        )
        source_binding = (
            record.storage_key == operation.source_key
            and (operation.expected_revision is None or record.revision == operation.expected_revision)
        )
        if operation.status == "prepared":
            if not same_image or not title_matches:
                raise DatabaseError("rename_target_changed")
            if not source_binding and not already_finalized:
                # 文件动作尚未被本 operation 可靠确认，同图手动改名已经替换了
                # source key 时可以补偿操作；其它组合必须停在 blocked。
                if not source_ok and not target_ok:
                    self._set_status(operation, "compensated", error={"error": "storage_key_changed"}, session=session)
                    counts["compensated"] += 1
                    return
                raise DatabaseError("rename_target_changed")
        if operation.status == "prepared" and source_ok and not target_ok:
            self.blob_store.link_move(operation.source_key, operation.target_key)
            source_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not source_ok:
            # 只有数据库仍保留 operation 记录的 CAS 输入时才能补交 Meme；若
            # finalize 已经成功但连接在提交后断开，则识别已完成事实而不重复递增
            # revision；人工改名或 SHA 变化必须阻断，不能覆盖用户结果。
            if (
                record.storage_key == operation.target_key
                and operation.expected_revision is not None
                and record.revision == operation.expected_revision + 1
                and same_image
                and title_matches
            ):
                self._set_status(operation, "file_applied", session=session)
            elif (
                record.storage_key == operation.source_key
                and (operation.expected_revision is None or record.revision == operation.expected_revision)
                and same_image
                and title_matches
            ):
                self._set_status(operation, "file_applied", session=session)
                record.storage_key = operation.target_key
                record.revision += 1
                record.updated_at = utcnow()
            else:
                raise DatabaseError("rename_target_changed")
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif source_ok and not target_ok and operation.status == "file_applied":
            # ``file_applied`` 已经声明发生过文件副作用；源文件重新出现且目标
            # 消失无法证明是回滚还是外部修改，不能把这条事实静默标成 compensated。
            raise DatabaseError("rename_recovery_ambiguous")
        else:
            raise DatabaseError("rename_recovery_ambiguous")

    def _recover_delete(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复隔离删除；冲突时阻断自动修改。"""
        assert operation.source_key and operation.target_key
        source_ok = self.blob_store.exists_with_identity(operation.source_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        quarantine_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        if operation.status == "prepared" and source_ok and not quarantine_ok:
            self.blob_store.quarantine(operation.source_key, token=UUID(operation.operation_token.hex))
            source_ok, quarantine_ok = False, True
            counts["retried"] += 1
        if quarantine_ok and not source_ok:
            self._set_status(operation, "file_applied", session=session)
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
            if record is not None:
                operation.meme_id = None
                self._session.delete(record)
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
            try:
                self.blob_store.unlink(operation.target_key)
            except DatabaseError:
                pass
        elif not quarantine_ok and not source_ok:
            self._set_status(operation, "blocked", error={"error": "delete_blob_missing", "message": "源文件和隔离文件均不存在"}, session=session)
            counts["blocked"] += 1
        else:
            raise DatabaseError("delete_recovery_ambiguous")

    def flat_preflight(self) -> dict[str, Any]:
        """只读检查业务 key、嵌套图片和记录/文件一致性，供 migration 与启动门禁使用。"""
        report: dict[str, Any] = {"non_flat_keys": [], "nested_images": [], "orphan_files": [], "missing_files": [], "mismatched": [], "active_operations": []}
        with self.resources.factory() as session:
            records = list(session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id)))
            referenced: set[str] = set()
            for record in records:
                referenced.add(record.storage_key)
                try:
                    validate_business_storage_key(record.storage_key)
                except ValueError:
                    report["non_flat_keys"].append(record.storage_key)
                if not self.blob_store.exists_with_identity(record.storage_key):
                    report["missing_files"].append(str(record.id))
                elif not self.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes):
                    report["mismatched"].append(str(record.id))
            for path in self.blob_store.root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                    continue
                key = path.relative_to(self.blob_store.root).as_posix()
                if "/" in key and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["nested_images"].append(key)
                elif key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["orphan_files"].append(key)
            operations = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE)))))
            for operation in operations:
                fields = []
                if operation.operation_type == "upload":
                    fields = [operation.target_key]
                elif operation.operation_type == "rename":
                    fields = [operation.source_key, operation.target_key]
                elif operation.operation_type == "delete":
                    fields = [operation.source_key]
                for value in fields:
                    if value:
                        try:
                            validate_business_storage_key(value)
                        except ValueError:
                            report["non_flat_keys"].append(value)
            report["active_operations"] = [str(item.id) for item in operations]
        return report

    def integrity_scan(self) -> dict[str, Any]:
        """双向核对数据库 Meme 与文件对象，标记缺失/指纹冲突并报告孤立文件。"""
        report: dict[str, Any] = {"orphan_files": [], "missing_files": [], "mismatched": [], "path_conflicts": [], "active_operations": []}
        with self.resources.factory() as session:
            records = list(session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id)))
            referenced: set[str] = set()
            duplicate_keys: dict[str, list[str]] = {}
            for record in records:
                referenced.add(record.storage_key)
                duplicate_keys.setdefault(record.storage_key, []).append(str(record.id))
                if not self.blob_store.exists_with_identity(record.storage_key):
                    report["missing_files"].append(str(record.id))
                    record.context_status = "repair_required"
                    continue
                if not self.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes):
                    report["mismatched"].append(str(record.id))
                    record.context_status = "repair_required"
            for path in self.blob_store.root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                    continue
                key = path.relative_to(self.blob_store.root).as_posix()
                if key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["orphan_files"].append(key)
            report["path_conflicts"] = [ids for ids in duplicate_keys.values() if len(ids) > 1]
            report["active_operations"] = [str(item.id) for item in session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE))))]
            session.commit()
        return report
