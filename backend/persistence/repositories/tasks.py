"""任务队列与批次持久化 Repository。

该模块位于 persistence Repository 边界，只负责 scope-bound 任务事实、lane/fairness、
claim/lease fencing 和批次收束；Worker、HTTP、图片文件和外部进程由其它模块负责。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import uuid
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.agent_resume import append_error_history, append_task_error_history, normalize_identifier, sanitize_error
from executor.agent_limits import validate_agent_concurrency, validate_agent_concurrency_at_most
from backend.persistence.engine import DatabaseError
from backend.persistence.models import (
    Meme,
    Scope,
    ScopeContext,
    Task,
    TaskBatch,
    TaskBatchItem,
    TaskLaneFairness,
    TaskLaneResourceSlot,
    TaskLaneSlot,
    GLOBAL_LANE_RESOURCE_KEY,
    utcnow,
)


# 图片 pipeline 的显式阶段任务由专用控制面推进；通用 Agent fair claim 不应抢走这些带
# 可信 submission_mode 的叶子任务。
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
        limit = validate_agent_concurrency_at_most(
            scope_capacity,
            capacity,
            error_code="agent_scope_concurrency_exceeds_global",
        ) if scope_capacity is not None else None
    except ValueError as exc:
        raise DatabaseError("agent_claim_config_invalid") from exc
    return capacity, limit


def validate_lane_resource_key(value: object | None) -> str:
    """校验并规范化不透明资源 key，供任务提交和 claim 共用。"""
    if value is None:
        return GLOBAL_LANE_RESOURCE_KEY
    if not isinstance(value, str):
        raise ValueError("agent_resource_key_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("agent_resource_key_invalid")
    return normalized


def _validate_resource_capacity(resource_capacity: object | None, lane_capacity: int) -> int:
    """校验资源槽位容量，并确保资源池不会放宽全局 lane 上限。"""
    try:
        value = lane_capacity if resource_capacity is None else validate_agent_concurrency_at_most(
            resource_capacity,
            lane_capacity,
            error_code="agent_resource_concurrency_exceeds_lane",
        )
    except (TypeError, ValueError) as exc:
        raise DatabaseError("agent_claim_config_invalid") from exc
    return int(value)


def validate_lane_resource_concurrency(value: object | None, lane_capacity: int) -> dict[str, int]:
    """校验资源 key 到运行容量的映射，缺失 key 由调用方继承全局容量。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("agent_resource_concurrency_invalid")
    result: dict[str, int] = {}
    for raw_key, raw_capacity in value.items():
        try:
            key = validate_lane_resource_key(raw_key)
            capacity = validate_agent_concurrency_at_most(
                raw_capacity,
                lane_capacity,
                error_code="agent_resource_concurrency_exceeds_lane",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("agent_resource_concurrency_invalid") from exc
        if key in result:
            raise ValueError("agent_resource_key_duplicate")
        result[key] = int(capacity)
    return result


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
        lane_backpressure_scope_id: str | None = None,
        lane_resource_key: str | None = None,
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
        try:
            lane_resource_key = validate_lane_resource_key(lane_resource_key)
        except ValueError as exc:
            raise DatabaseError(exc.args[0]) from exc
        # Agent 任务先锁定 lane，再检查活动去重，避免策略请求并发穿过预检窗口。
        if lane == "agent" or lane_backpressure is not None:
            self._lock_lane(lane)
        if dedupe_key:
            existing = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))).with_for_update())
            if existing:
                try:
                    existing_resource = validate_lane_resource_key(getattr(existing, "lane_resource_key", None))
                except ValueError as exc:
                    raise DatabaseError("agent_resource_key_invalid") from exc
                if existing_resource != lane_resource_key:
                    raise DatabaseError("task_resource_conflict")
                return existing
        # Agent 队列不设 queued+running 容量门禁；其它独立 lane（例如缩略图）
        # 仍可显式使用既有背压策略。
        if lane_backpressure is not None and lane != "agent":
            active_filters = [Task.lane == lane, Task.status.in_(("queued", "running"))]
            if lane_backpressure_scope_id is not None:
                active_filters.append(Task.scope_id == lane_backpressure_scope_id)
            active = self.session.scalar(select(func.count()).select_from(Task).where(*active_filters)) or 0
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
            lane_resource_key=lane_resource_key,
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
                    try:
                        existing_resource = validate_lane_resource_key(getattr(existing, "lane_resource_key", None))
                    except ValueError as exc:
                        raise DatabaseError("agent_resource_key_invalid") from exc
                    if existing_resource != lane_resource_key:
                        raise DatabaseError("task_resource_conflict")
                    return existing
            raise DatabaseError("task_submit_conflict")
        return task

    def _lock_lane(self, lane: str) -> None:
        """在当前事务中锁定整个 lane，保证跨进程槽位判断原子化。"""
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"mememeow:lane:{lane}"))))

    def _ensure_lane_slots(self, lane: str, capacity: int) -> None:
        """幂等创建 lane 槽位；调用者必须先持有 lane advisory lock。"""
        for number in range(max(1, int(capacity))):
            if self.session.get(TaskLaneSlot, (lane, number)) is None:
                self.session.add(TaskLaneSlot(lane=lane, slot_number=number))
        self.session.flush()

    def _ensure_lane_resource_slots(self, lane: str, resource_key: str, capacity: int) -> None:
        """幂等创建资源池槽位；调用者必须先持有同一 lane advisory lock。"""
        for number in range(max(1, int(capacity))):
            identity = (lane, resource_key, number)
            if self.session.get(TaskLaneResourceSlot, identity) is None:
                self.session.add(TaskLaneResourceSlot(lane=lane, resource_key=resource_key, slot_number=number))
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

    def _release_lane_resource_slot(self, task_scope_id: str, task_id: str, *, resource_key: str | None = None, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """按任务和 claim fencing 释放模型资源槽位。"""
        filters = [TaskLaneResourceSlot.task_scope_id == task_scope_id, TaskLaneResourceSlot.task_id == task_id]
        if resource_key is not None:
            filters.append(TaskLaneResourceSlot.resource_key == resource_key)
        slot = self.session.scalar(select(TaskLaneResourceSlot).where(*filters).with_for_update())
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

    def _release_lane_slots(self, task_scope_id: str, task_id: str, *, resource_key: str | None = None, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """在同一事务中释放全局槽位和资源槽位，并应用完整 claim fencing。"""
        global_slot = self.session.scalar(
            select(TaskLaneSlot)
            .where(TaskLaneSlot.task_scope_id == task_scope_id, TaskLaneSlot.task_id == task_id)
            .with_for_update()
        )
        resource_filters = [TaskLaneResourceSlot.task_scope_id == task_scope_id, TaskLaneResourceSlot.task_id == task_id]
        if resource_key is not None:
            resource_filters.append(TaskLaneResourceSlot.resource_key == resource_key)
        resource_slot = self.session.scalar(select(TaskLaneResourceSlot).where(*resource_filters).with_for_update())
        slots = [slot for slot in (global_slot, resource_slot) if slot is not None]
        if not slots:
            return False
        if any(owner is not None and slot.lease_owner not in {None, owner} for slot in slots):
            return False
        if any(claim_generation is not None and slot.claim_generation not in {None, claim_generation} for slot in slots):
            return False
        for slot in slots:
            slot.task_scope_id = None
            slot.task_id = None
            slot.lease_owner = None
            slot.claim_generation = None
            slot.lease_expires_at = None
        return True

    def slot_for_task(self, task_id: str) -> TaskLaneSlot | None:
        """读取当前 scope 任务占用的槽位，用于安全摘要和诊断。"""
        return self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id))

    def resource_slot_for_task(self, task_id: str) -> TaskLaneResourceSlot | None:
        """读取当前 scope 任务占用的资源槽位，供内部恢复和诊断使用。"""
        return self.session.scalar(select(TaskLaneResourceSlot).where(TaskLaneResourceSlot.task_scope_id == self.scope.scope_id, TaskLaneResourceSlot.task_id == task_id))

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
            self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=previous_owner, claim_generation=previous_generation)
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
            self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=owner)
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
        self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=None)
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

    def _claim_lane_resource_slot(self, task: Task, *, resource_key: str, owner: str, lease_expires_at: datetime, capacity: int) -> bool:
        """为任务分配资源池槽位；资源槽位与全局槽位由调用方在同一事务申请。"""
        self._lock_lane(task.lane)
        self._ensure_lane_resource_slots(task.lane, resource_key, capacity)
        now = utcnow()
        candidate = self.session.scalar(
            select(TaskLaneResourceSlot)
            .where(
                TaskLaneResourceSlot.lane == task.lane,
                TaskLaneResourceSlot.resource_key == resource_key,
                TaskLaneResourceSlot.slot_number < max(1, int(capacity)),
                (
                    (TaskLaneResourceSlot.task_id.is_(None))
                    | (TaskLaneResourceSlot.lease_expires_at.is_(None))
                    | (TaskLaneResourceSlot.lease_expires_at <= now)
                ),
            )
            .order_by(TaskLaneResourceSlot.slot_number)
            .limit(1)
        )
        if candidate is None:
            return False
        holder = None
        if candidate.task_id is not None:
            holder = self.session.scalar(
                select(Task)
                .where(Task.scope_id == candidate.task_scope_id, Task.id == candidate.task_id)
                .with_for_update()
            )
        slot = self.session.scalar(
            select(TaskLaneResourceSlot)
            .where(
                TaskLaneResourceSlot.lane == task.lane,
                TaskLaneResourceSlot.resource_key == resource_key,
                TaskLaneResourceSlot.slot_number == candidate.slot_number,
            )
            .with_for_update(skip_locked=True)
        )
        if slot is None:
            return False
        if slot.task_id != candidate.task_id or slot.task_scope_id != candidate.task_scope_id:
            return False
        if slot.task_id is not None and slot.lease_expires_at is not None and slot.lease_expires_at > now:
            return False
        if holder is not None and holder.status == "running":
            if holder.lease_expires_at is None or holder.lease_expires_at > now:
                raise DatabaseError("agent_lane_resource_slot_inconsistent")
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
            self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=previous_owner, claim_generation=previous_generation)
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

    def _clear_expired_lane_resource_slots_locked(self, *, lane: str, resource_key: str, now: datetime, capacity: int) -> None:
        """清理指定资源池的失效槽位，并拒绝有效租约不一致状态。"""
        candidates = list(
            self.session.scalars(
                select(TaskLaneResourceSlot)
                .where(
                    TaskLaneResourceSlot.lane == lane,
                    TaskLaneResourceSlot.resource_key == resource_key,
                    TaskLaneResourceSlot.slot_number < max(1, int(capacity)),
                    TaskLaneResourceSlot.task_id.is_not(None),
                    (
                        TaskLaneResourceSlot.lease_expires_at.is_(None)
                        | (TaskLaneResourceSlot.lease_expires_at <= now)
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
                select(TaskLaneResourceSlot)
                .where(
                    TaskLaneResourceSlot.lane == lane,
                    TaskLaneResourceSlot.resource_key == resource_key,
                    TaskLaneResourceSlot.slot_number == candidate.slot_number,
                )
                .with_for_update()
            )
            if slot is None or slot.task_id != candidate.task_id or slot.task_scope_id != candidate.task_scope_id:
                continue
            if slot.lease_expires_at is None or slot.lease_expires_at > now:
                continue
            if holder is not None and holder.status == "running":
                if holder.lease_expires_at is None or holder.lease_expires_at > now:
                    raise DatabaseError("agent_lane_resource_slot_inconsistent")
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
        resource_key: str | None = None,
        resource_capacity: int | None = None,
        scope_capacity: int | None = None,
        scope_id: str | None = None,
        exclude_task_types: set[str] | frozenset[str] | None = None,
        exclude_image_pipeline: bool = False,
    ) -> Task | None:
        """在 lane 事务内按最久未服务 scope 公平认领一个任务。

        ``scope_id`` 只供内部恢复或测试缩小候选范围；正常 Agent manager 必须
        省略它，让候选 scope 完全来自持久 Task.scope_id。客户端 payload、user_id
        和进程内 cursor 不参与选择。返回的 Task.scope_id 是后续 facade 装配的
        唯一可信归属；全局 slot、资源 slot、公平状态、claim generation 和 lease 会
        在本事务一起提交，任一步失败都会由 UnitOfWork 回滚。资源 key 只作为不透明
        分区标识，队列数量不参与 claim 拒绝。
        """
        if not isinstance(owner, str) or not owner.strip():
            raise DatabaseError("agent_claim_owner_invalid")
        if not isinstance(lane, str) or not lane.strip() or len(lane) > 64:
            raise DatabaseError("agent_claim_lane_invalid")
        try:
            capacity, limit = _validate_lane_capacities(lane_capacity, scope_capacity)
            assert capacity is not None
            resource_key = validate_lane_resource_key(resource_key)
            resource_capacity = _validate_resource_capacity(resource_capacity, capacity)
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
            self._ensure_lane_resource_slots(lane, resource_key, resource_capacity)
            self._recover_expired_lane_locked(lane=lane, now=now, scope_id=scope_id, exclude_task_types=exclude_task_types)
            self._clear_expired_lane_slots_locked(lane=lane, now=now, capacity=capacity)
            self._clear_expired_lane_resource_slots_locked(lane=lane, resource_key=resource_key, now=now, capacity=resource_capacity)

            candidate_filters = [Task.lane == lane, Task.status == "queued", Task.available_at <= now]
            resource_filter = Task.lane_resource_key == resource_key
            if resource_key == GLOBAL_LANE_RESOURCE_KEY:
                resource_filter = or_(resource_filter, Task.lane_resource_key.is_(None))
            candidate_filters.append(resource_filter)
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

            # 公平行按 lane/resource/scope 唯一键惰性建立，初次平局交给 Scope.created_at 和
            # scope ID，动态加入的 scope 不依赖任何进程内状态获得确定顺序。
            for candidate_scope in candidate_scope_ids:
                rows = list(
                    self.session.scalars(
                        select(TaskLaneFairness)
                        .where(TaskLaneFairness.lane == lane, TaskLaneFairness.resource_key == resource_key, TaskLaneFairness.scope_id == candidate_scope)
                        .with_for_update()
                    )
                )
                if len(rows) > 1:
                    raise DatabaseError("agent_fairness_unavailable")
                if not rows:
                    self.session.add(TaskLaneFairness(lane=lane, resource_key=resource_key, scope_id=candidate_scope, last_dispatch_sequence=0))
            self.session.flush()
            fairness_rows = list(
                self.session.scalars(
                    select(TaskLaneFairness).where(TaskLaneFairness.lane == lane, TaskLaneFairness.resource_key == resource_key).with_for_update()
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
                task_filters.append(resource_filter)
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
                    # lane 已由本事务独占；无 slot 只表示运行容量暂满，公平序号不得推进。
                    return None
                if not self._claim_lane_resource_slot(task, resource_key=resource_key, owner=owner, lease_expires_at=expires_at, capacity=resource_capacity):
                    # 全局 slot 已先取得；显式释放后再返回 queued，避免部分 claim
                    # 在正常返回路径中提交。异常路径仍由事务整体回滚。
                    self._release_lane_slot(task.scope_id, task.id, owner=owner)
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
                resource_slot = self.session.scalar(
                    select(TaskLaneResourceSlot)
                    .where(
                        TaskLaneResourceSlot.lane == task.lane,
                        TaskLaneResourceSlot.resource_key == resource_key,
                        TaskLaneResourceSlot.task_scope_id == task.scope_id,
                        TaskLaneResourceSlot.task_id == task.id,
                    )
                    .with_for_update()
                )
                if resource_slot is None:
                    raise DatabaseError("agent_lane_resource_slot_inconsistent")
                resource_slot.claim_generation = task.claim_generation
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

    def claim(self, *, owner: str, lease_seconds: int = 120, lane: str | None = None, task_id: str | None = None, lane_capacity: int | None = None, resource_key: str | None = None, resource_capacity: int | None = None, scope_capacity: int | None = None, exclude_task_types: set[str] | frozenset[str] | None = None) -> Task | None:
        """兼容 scope-bound 认领并递增 claim generation。

        Agent 正常调度必须使用 ``claim_next``；这个入口只保留给已由专用控制面
        选定的任务、租约恢复和历史兼容路径。lane 任务仍持有 advisory lock，
        因而不能绕过全局 slot、资源 slot 或可选 scope 上限。传入的资源 key
        必须与持久任务事实一致；省略时从任务列恢复。
        """
        now = utcnow()
        requested_resource_key = resource_key
        if lane and lane_capacity is not None:
            lane_capacity, scope_capacity = _validate_lane_capacities(lane_capacity, scope_capacity)
            assert lane_capacity is not None
            try:
                if requested_resource_key is not None:
                    resource_key = validate_lane_resource_key(requested_resource_key)
            except ValueError as exc:
                raise DatabaseError(exc.args[0]) from exc
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
        if requested_resource_key is not None:
            requested_resource_filter = Task.lane_resource_key == resource_key
            if resource_key == GLOBAL_LANE_RESOURCE_KEY:
                requested_resource_filter = or_(requested_resource_filter, Task.lane_resource_key.is_(None))
            filters.append(requested_resource_filter)
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
            if requested_resource_key is not None:
                requested_resource_filter = Task.lane_resource_key == resource_key
                if resource_key == GLOBAL_LANE_RESOURCE_KEY:
                    requested_resource_filter = or_(requested_resource_filter, Task.lane_resource_key.is_(None))
                filters.append(requested_resource_filter)
            if exclude_task_types:
                filters.append(~Task.task_type.in_(exclude_task_types))
            statement = select(Task).where(*filters).order_by(Task.lease_expires_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
            task = self.session.scalar(statement)
        if task is None:
            return None
        if lane and lane_capacity is not None:
            try:
                selected_resource_key = validate_lane_resource_key(getattr(task, "lane_resource_key", None))
            except ValueError as exc:
                raise DatabaseError(exc.args[0]) from exc
            if requested_resource_key is not None and selected_resource_key != resource_key:
                raise DatabaseError("agent_claim_resource_mismatch")
            resource_key = selected_resource_key
            selected_resource_capacity = _validate_resource_capacity(resource_capacity, lane_capacity)
            self._ensure_lane_resource_slots(lane, resource_key, selected_resource_capacity)
            self._clear_expired_lane_resource_slots_locked(lane=lane, resource_key=resource_key, now=now, capacity=selected_resource_capacity)
            # task_id 过滤只缩小候选范围，不能绕过全局 lane 槽位上限。
            expires_at = now + timedelta(seconds=lease_seconds)
            if not self._claim_lane_slot(task, owner=owner, lease_expires_at=expires_at, capacity=lane_capacity):
                return None
            if not self._claim_lane_resource_slot(task, resource_key=resource_key, owner=owner, lease_expires_at=expires_at, capacity=selected_resource_capacity):
                self._release_lane_slot(task.scope_id, task.id, owner=owner)
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
            resource_slot = self.session.scalar(
                select(TaskLaneResourceSlot)
                .where(
                    TaskLaneResourceSlot.lane == task.lane,
                    TaskLaneResourceSlot.resource_key == resource_key,
                    TaskLaneResourceSlot.task_scope_id == task.scope_id,
                    TaskLaneResourceSlot.task_id == task.id,
                )
                .with_for_update()
            )
            if resource_slot is None:
                raise DatabaseError("agent_lane_resource_slot_inconsistent")
            resource_slot.claim_generation = task.claim_generation
        self.session.flush()
        return task

    def heartbeat(self, task_id: str, claim_generation: int, owner: str, lease_seconds: int = 120) -> bool:
        """在当前 claim 仍有效时续租，防止长时间外部调用被误判为崩溃。"""
        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
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
        slot = self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id).with_for_update())
        resource_slot = self.session.scalar(
            select(TaskLaneResourceSlot)
            .where(TaskLaneResourceSlot.task_scope_id == self.scope.scope_id, TaskLaneResourceSlot.task_id == task_id)
            .with_for_update()
        )
        if task.lane == "agent":
            if slot is None or resource_slot is None:
                return False
            if slot.lease_owner != owner or slot.claim_generation not in {None, claim_generation}:
                return False
            if resource_slot.lease_owner != owner or resource_slot.claim_generation not in {None, claim_generation}:
                return False
        task.lease_expires_at = expires_at
        task.updated_at = now
        if slot is not None and slot.lease_owner == owner and slot.claim_generation in {None, claim_generation}:
            slot.lease_expires_at = expires_at
        if resource_slot is not None and resource_slot.lease_owner == owner and resource_slot.claim_generation in {None, claim_generation}:
            resource_slot.lease_expires_at = expires_at
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
            self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=owner, claim_generation=claim_generation)
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
            self._release_lane_slots(task.scope_id, task.id, resource_key=task.lane_resource_key, owner=owner, claim_generation=claim_generation)
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
            self._release_lane_slots(self.scope.scope_id, task_id, owner=owner, claim_generation=claim_generation)
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
                            from backend.persistence.repositories.reverse_image import ReverseImageUsageRepository
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
        self._release_lane_slots(self.scope.scope_id, task_id, resource_key=task.lane_resource_key, owner=owner, claim_generation=claim_generation)
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



__all__ = ["IMAGE_PROCESSING_LANE_TYPES", "TaskRepository", "_validate_lane_capacities", "validate_lane_resource_key", "validate_lane_resource_concurrency"]
