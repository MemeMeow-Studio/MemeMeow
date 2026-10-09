from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping
from uuid import uuid4

from sqlalchemy import func, or_, select
from opentelemetry import metrics
from loguru import logger as diagnostic_logger

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
    TaskLaneResourceSlot,
    TaskLaneSlot,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.persistence.models import utcnow
from backend.agent_resume import append_error_history, append_task_error_history
from backend.config import validate_agent_concurrency
from executor.agent_limits import validate_agent_concurrency_at_most
from backend.tasks import IMAGE_PROCESSING_TASK_TYPES
from backend.metadata import MetadataError
from backend.scope import validate_scope_services
from backend.persistence.repositories.tasks import validate_lane_resource_concurrency, validate_lane_resource_key

# Worker 的调度和恢复日志继续归入旧 facade logger。
logger = logging.getLogger("backend.pg_services")


def _generic_task_filter() -> Any:
    """返回通用 Worker 可处理的任务条件，保留迁移前图片任务。"""
    # submission_mode 允许为 NULL 以兼容迁移前任务；直接对组合条件取反会让
    # SQL 的三值逻辑把这些历史行错误地过滤掉。
    return (Task.task_type != "image_name_generation") & or_(
        Task.submission_mode.is_(None),
        ~(
            Task.task_type.in_(IMAGE_PROCESSING_TASK_TYPES)
            & Task.submission_mode.in_(("pipeline", "standalone"))
        ),
    )

class PostgresTaskWorkerManager:
    """进程级任务协调器，统一管理线程池、处理器注册和任务恢复扫描。

    任务的数据库操作仍由 scope-bound ``PostgresTaskService`` 执行；协调器只按任务
    ID 调度工作，并在真正认领后根据持久 ``Task.scope_id`` 创建轻量服务视图。这样
    历史 scope 数量不会复制 Worker、线程池、handler registry 或全局 Agent lane。
    """

    def __init__(
        self,
        resources: DatabaseResources,
        *,
        agent_concurrency: int = 1,
        scope_concurrency: int | None = None,
        resource_concurrency: Mapping[str, int] | None = None,
        agent_backpressure: int | None = None,
        settings_version: str | None = None,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        """创建进程级线程池和任务协调状态，等待宿主安装 service resolver。

        ``agent_backpressure`` 仅为旧调用方保留，不参与 Agent 运行槽位或队列判定。
        """
        del agent_backpressure
        self.resources = resources
        self.agent_concurrency = validate_agent_concurrency(agent_concurrency)
        self.agent_scope_concurrency = validate_agent_concurrency_at_most(
            scope_concurrency if scope_concurrency is not None else 1,
            self.agent_concurrency,
            error_code="agent_scope_concurrency_exceeds_global",
        )
        self.resource_concurrency = validate_lane_resource_concurrency(resource_concurrency, self.agent_concurrency)
        self.settings_version = settings_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._executor = executor or ThreadPoolExecutor(max_workers=max(2, self.agent_concurrency + 1), thread_name_prefix="mememeow-scope-worker")
        self._owns_executor = executor is None
        self._service_resolver: Callable[[str], Any] | None = None
        self._scope_service_resolver: Callable[[str | ScopeContext], Any] | None = None
        self._handlers: dict[str, Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]] = {}
        self._lock = Lock()
        self._stopped = Event()
        self._started = False
        self._recovery_thread: Thread | None = None
        self._maintenance: list[Callable[[], None]] = []
        self._scheduled: set[str] = set()
        self._queue_scan_lock = Lock()
        self._telemetry_queue_labels: set[tuple[str, str]] = set()
        self._telemetry_snapshot: tuple[metrics.Observation, ...] = ()
        self._telemetry_priority = int(uuid4().hex[:13], 16)
        metrics.get_meter("mememeow.tasks").create_observable_gauge("task_scheduler_snapshot", callbacks=[self._observe_scheduler])
        self.owner = f"worker-{os.getpid()}-{id(self)}"

    def resource_capacity(self, resource_key: str | None) -> int:
        """返回资源 key 的运行容量，缺省资源继承全局 Agent 容量。"""
        try:
            key = validate_lane_resource_key(resource_key)
        except ValueError as exc:
            raise DatabaseError("agent_resource_key_invalid") from exc
        return self.resource_concurrency.get(key, self.agent_concurrency)

    @property
    def stopping(self) -> bool:
        """告知协作式处理器进程正在关闭，允许保存进度后结束执行。"""
        return self._stopped.is_set()

    @property
    def worker_count(self) -> int:
        """返回当前进程协调器数量；一个 manager 代表一个 Worker 控制面。"""
        return 0 if self._stopped.is_set() else 1

    @property
    def executor(self) -> ThreadPoolExecutor:
        """返回共享线程池，供 scope facade 复用而不自行创建调度资源。"""
        return self._executor

    def set_service_resolvers(self, task_resolver: Callable[[str], Any], scope_resolver: Callable[[str | ScopeContext], Any]) -> None:
        """安装按持久任务或显式 scope 创建轻量服务视图的回调。"""
        self._service_resolver = task_resolver
        self._scope_service_resolver = scope_resolver

    def register(self, task_type: str, handler: Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]) -> None:
        """在进程级 registry 注册一个任务处理器，所有 scope 共用该注册表。"""
        self._handlers[task_type] = handler

    def handler(self, task_type: str) -> Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any] | None:
        """读取当前任务类型的全局处理器。"""
        return self._handlers.get(task_type)

    def start(self) -> dict[str, list[str]]:
        """执行一次全局租约恢复、批次恢复和 queued 任务扫描。"""
        with self._lock:
            if self._started:
                return {"started": [], "invalid_tasks": []}
            self._started = True
        try:
            queued = self._recover_expired()
            invalid = self._fail_invalid_scope_tasks()
            queued.extend(self._recover_pending_batches())
            with self.resources.factory() as session:
                queued.extend(
                    session.scalars(
                    select(Task.id).where(
                            Task.status == "queued",
                            _generic_task_filter(),
                        )
                    )
                )
            for task_id in dict.fromkeys(queued):
                self.schedule(task_id)
            self._recovery_thread = Thread(target=self._recovery_loop, name="mememeow-task-recovery", daemon=True)
            self._recovery_thread.start()
            return {"started": [self.owner], "invalid_tasks": sorted(set(invalid))}
        except Exception:
            with self._lock:
                self._started = False
            raise

    def _recovery_loop(self) -> None:
        """持续恢复过期租约和已到执行时间的任务，覆盖启动后才过期的任务。"""
        while not self._stopped.wait(5):
            try:
                self._recover_expired()
                self._schedule_queued()
                for maintain in self._maintenance:
                    maintain()
            except Exception as exc:
                diagnostic_logger.exception("task_recovery_error error_type={}", type(exc).__name__)

    def register_maintenance(self, callback: Callable[[], None]) -> None:
        """注册短时持久任务维护操作，与恢复线程共用生命周期。"""
        self._maintenance.append(callback)

    def _recover_expired(self) -> list[str]:
        """跨所有 scope 恢复过期 claim，并释放旧 lane 槽位。"""
        now = utcnow()
        queued: list[str] = []
        with self.resources.factory() as session:
            rows = list(
                session.scalars(
                    select(Task)
                    .where(
                        Task.status == "running",
                        Task.lease_expires_at < now,
                        _generic_task_filter(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(5000)
                )
            )
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
                self._release_slot(session, task.scope_id, task.id, owner=previous_owner, claim_generation=previous_generation)
            session.commit()
        return queued

    @staticmethod
    def _release_slot(session: Any, scope_id: str, task_id: str, *, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """在全局恢复事务中按完整 claim 释放全局和资源槽位。"""
        slot = session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == scope_id, TaskLaneSlot.task_id == task_id).with_for_update())
        resource_slot = session.scalar(
            select(TaskLaneResourceSlot)
            .where(TaskLaneResourceSlot.task_scope_id == scope_id, TaskLaneResourceSlot.task_id == task_id)
            .with_for_update()
        )
        slots = [candidate for candidate in (slot, resource_slot) if candidate is not None]
        if not slots:
            return False
        if any(owner is not None and candidate.lease_owner not in {None, owner} for candidate in slots):
            logger.info("task_lane_fencing_rejection task=%s scope=%s", task_id, scope_id)
            return False
        if any(claim_generation is not None and getattr(candidate, "claim_generation", None) not in {None, claim_generation} for candidate in slots):
            logger.info("task_lane_fencing_rejection task=%s scope=%s", task_id, scope_id)
            return False
        for candidate in slots:
            candidate.task_scope_id = None
            candidate.task_id = None
            candidate.lease_owner = None
            if hasattr(candidate, "claim_generation"):
                candidate.claim_generation = None
            candidate.lease_expires_at = None
        return True

    def _fail_invalid_scope_tasks(self) -> list[str]:
        """启动扫描发现非法持久 scope 时稳定失败，绝不猜测为 local。"""
        invalid: list[str] = []
        with self.resources.factory() as session:
            rows = list(
                session.scalars(
                    select(Task).where(
                        Task.status.in_(("queued", "running")),
                        _generic_task_filter(),
                    )
                )
            )
            for task in rows:
                try:
                    ScopeContext(task.scope_id)
                except (TypeError, ValueError):
                    invalid.append(task.id)
                    task.status = "failed"
                    task.completed_at = utcnow()
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.error = append_task_error_history(
                        task,
                        {"error": "task_scope_invalid", "message": "任务缺少有效 scope"},
                        attempt=task.attempt_count,
                        executor_attempt_id=task.executor_attempt_id,
                        session_id=task.resume_session_id,
                        occurred_at=utcnow().isoformat(),
                    )
                    task.message = "任务 scope 无效"
                    self._release_slot(session, task.scope_id, task.id)
            session.commit()
        return invalid

    def _recover_pending_batches(self) -> list[str]:
        """按批次所属 scope 恢复未完成的结果汇总。"""
        queued: list[str] = []
        if self._scope_service_resolver is None:
            return queued
        with self.resources.factory() as session:
            batches = list(session.execute(select(TaskBatch.scope_id, TaskBatch.batch_id).where(TaskBatch.sealed.is_(True), TaskBatch.finalizer_state.in_(('pending', 'submitted'))).limit(5000)).all())
        for scope_id, batch_id in batches:
            try:
                services = self._scope_service_resolver(scope_id)
                with self.resources.environment(services.scope) as environment:
                    environment.tasks.finalize_batch(batch_id)
            except (DatabaseError, RuntimeError, TypeError, ValueError):
                # 任务 scope 无法装配时由后续任务诊断和宿主日志收束，不回退到 local。
                continue
        return queued

    def schedule(self, task_id: str) -> None:
        """将任务加入进程级调度集合，避免不同 scope 重复提交同一 future。"""
        with self._lock:
            if self._stopped.is_set() or task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._executor.submit(self._run, task_id)

    def _run(self, task_id: str) -> None:
        """先按持久 scope 认领任务，再创建 scope facade 执行处理器。"""
        service = None
        claim = None
        try:
            if self._stopped.is_set():
                return
            claim = self._claim_for_task(task_id)
            if claim is None:
                self._task_finished(task_id, claimed=False)
                return
            if self._scope_service_resolver is None:
                raise RuntimeError("task_scope_unavailable")
            # Worker 认领后仍必须复用统一 factory 校验；宿主自定义 resolver 不能以
            # 返回错误 scope 的 facade 绕过 claim 的业务隔离边界。
            services = validate_scope_services(ScopeContext(claim.scope_id), self._scope_service_resolver(claim.scope_id))
            service = getattr(services, "tasks", None)
            if service is None:
                raise RuntimeError("task_scope_unavailable")
            # fair claim 可能选择另一个 scope 的 queued Task；以实际 claim ID
            # 执行，不能把原唤醒 token 当作持久任务归属。
            service._run(claim.id, preclaimed=claim)
            if claim.id != task_id:
                # 公平 claim 可能由一个 queued 唤醒 token 认领另一个 scope 的任务；
                # 清除原 token，避免它永久占据进程内 scheduled 集合。
                self._task_finished(task_id, claimed=False)
        except Exception as exc:
            if service is None:
                # 工厂异常发生在 claim 之后时只能用完整 claim fencing 收束；
                # 没有 claim 证据则不按裸 task_id 修改任意任务。
                if isinstance(exc, DatabaseError) and exc.code == "agent_fairness_unavailable":
                    logger.error("agent_fairness_unavailable task=%s", task_id)
                    self._mark_fairness_unavailable(task_id)
                self._fail_unresolvable(claim)
                self._task_finished(task_id, claimed=claim is not None)
            else:
                # 业务 facade 异常不能遗留 scheduled 标记，否则恢复扫描无法再次唤醒任务。
                self._task_finished(task_id, claimed=claim is not None)

    def _claim_for_task(self, task_id: str) -> Task | None:
        """从任务控制面读取 scope 并在创建业务 facade 前完成 claim。"""
        with self.resources.factory() as session:
            queued_row = session.scalar(select(Task).where(Task.id == task_id))
            scope_id = queued_row.scope_id if queued_row is not None else None
        if not isinstance(scope_id, str) or not scope_id:
            return None
        try:
            scope = ScopeContext(scope_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("task_scope_invalid") from exc
        with self.resources.environment(scope) as environment:
            queued = environment.tasks.get(task_id)
            if queued is None:
                return None
            if queued.task_type == "image_name_generation":
                return None
            if queued.task_type in IMAGE_PROCESSING_TASK_TYPES and queued.submission_mode in {"pipeline", "standalone"}:
                # 图片任务属于专用 Worker；通用 manager 不得认领或收束它们。
                return None
            if queued.lane == "agent":
                claim = environment.tasks.claim_next(
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                    lane="agent",
                    lane_capacity=self.agent_concurrency,
                    resource_key=queued.lane_resource_key,
                    resource_capacity=self.resource_capacity(queued.lane_resource_key),
                    scope_capacity=self.agent_scope_concurrency,
                    exclude_image_pipeline=True,
                )
                if claim is not None:
                    logger.info(
                        "agent_fair_claim task=%s scope=%s generation=%s",
                        claim.id,
                        claim.scope_id,
                        claim.claim_generation,
                    )
                return claim
            return environment.tasks.claim(
                owner=self.owner,
                lease_seconds=self.lease_seconds,
                task_id=task_id,
                lane=queued.lane,
                lane_capacity=self.agent_concurrency if queued.lane == "agent" else None,
                resource_key=queued.lane_resource_key if queued.lane == "agent" else None,
                resource_capacity=self.resource_capacity(queued.lane_resource_key) if queued.lane == "agent" else None,
                scope_capacity=self.agent_scope_concurrency if queued.lane == "agent" else None,
            )

    def _mark_fairness_unavailable(self, task_id: str) -> None:
        """保留 queued 任务并记录稳定调度错误，禁止降级竞争式 claim。"""
        with self.resources.factory() as session:
            task = session.scalar(
                select(Task).where(Task.id == task_id, Task.status == "queued").with_for_update()
            )
            if task is not None and task.lane == "agent":
                task.error = {"error": "agent_fairness_unavailable", "message": "Agent 公平调度状态不可用"}
                task.message = "Agent 公平调度不可用，任务保持排队"
                task.updated_at = utcnow()
            session.commit()

    def _fail_unresolvable(self, claim: Task | None) -> None:
        """按完整 claim 收束 scope 装配失败，旧 claim 不得终止新 Worker。"""
        if claim is None:
            return
        with self.resources.factory() as session:
            now = utcnow()
            statement = select(Task).where(
                Task.scope_id == claim.scope_id,
                Task.id == claim.id,
                Task.status == "running",
                Task.claim_generation == claim.claim_generation,
                Task.lease_owner == self.owner,
                Task.lease_expires_at > now,
            ).with_for_update()
            task = session.scalar(statement)
            if task is None:
                logger.info("task_scope_assembly_fencing_rejection task=%s scope=%s generation=%s", claim.id, claim.scope_id, claim.claim_generation)
                session.commit()
                return
            task.status = "failed"
            task.completed_at = utcnow()
            task.lease_owner = None
            task.lease_expires_at = None
            task.message = "任务 scope 无法装配"
            task.error = append_task_error_history(
                task,
                {"error": "task_scope_unavailable", "message": "任务 scope 当前不可用"},
                attempt=getattr(task, "attempt_count", 0),
                executor_attempt_id=getattr(task, "executor_attempt_id", None),
                session_id=getattr(task, "resume_session_id", None),
                occurred_at=utcnow().isoformat(),
            )
            self._release_slot(session, task.scope_id, task.id, owner=self.owner, claim_generation=claim.claim_generation)
            session.commit()

    def _task_finished(self, task_id: str, *, claimed: bool) -> None:
        """释放全局调度标记，并在 lane 槽位释放后扫描下一批任务。"""
        with self._lock:
            self._scheduled.discard(task_id)
            stopped = self._stopped.is_set()
        if claimed and not stopped:
            self._schedule_queued()

    def _schedule_queued(self) -> None:
        """跨 scope 唤醒 queued 任务，保持全局运行槽位释放后的前进性。"""
        if self._stopped.is_set():
            return
        with self._queue_scan_lock:
            try:
                task_ids = self._queued_snapshot()
            except Exception:
                self._telemetry_snapshot = tuple(observation for observation in self._telemetry_snapshot if observation.attributes.get("measurement") != "source_error") + (metrics.Observation(1, {"scheduler_scope": "generic_tasks", "database_scope": "deployment", "measurement": "source_error"}),)
                raise
        for task_id in task_ids:
            self.schedule(task_id)

    def _observe_scheduler(self, options: Any) -> tuple[metrics.Observation, ...]:
        """SDK 从一次不可变快照同时取得数量、成功时间与范围；不读取数据库。"""
        return self._telemetry_snapshot

    def _queued_snapshot(self) -> list[str]:
        """读取已有队列扫描结果，按串行快照更新 gauge 并归还数据库连接。"""
        with self.resources.factory() as session:
            queue_counts = list(session.execute(select(Task.task_type, Task.lane, Task.status, func.count()).where(Task.status.in_(("queued", "running")), _generic_task_filter()).group_by(Task.task_type, Task.lane, Task.status)))
            task_ids = list(
                session.scalars(
                    select(Task.id)
                    .where(
                        Task.status == "queued",
                        Task.available_at <= utcnow(),
                        _generic_task_filter(),
                    )
                    .order_by(Task.available_at, Task.id)
                    .limit(500)
                )
            )
        meter = metrics.get_meter("mememeow.tasks")
        queue_gauge = meter.create_gauge("task_queued", unit="{task}")
        current_labels = {(task_type, lane) for task_type, lane, status, count in queue_counts}
        self._telemetry_queue_labels |= current_labels
        counts = {(task_type, lane, status): count for task_type, lane, status, count in queue_counts}
        scope = {"scheduler_scope": "generic_tasks", "database_scope": "deployment"}
        observations = [metrics.Observation(time.time(), {**scope, "measurement": "observed_at"})]
        observations.extend((metrics.Observation(self._telemetry_priority, {**scope, "measurement": "instance_priority"}), metrics.Observation(0, {**scope, "measurement": "source_error"})))
        for status in ("queued", "running"):
            observations.append(metrics.Observation(sum(count for (task_type, lane, state), count in counts.items() if state == status), {**scope, "measurement": status + "_total"}))
        for task_type, lane in sorted(self._telemetry_queue_labels):
            labels = {"task_type": task_type, "lane": lane}
            queue_gauge.set(counts.get((task_type, lane, "queued"), 0), labels)
            for status in ("queued", "running"):
                observations.append(metrics.Observation(counts.get((task_type, lane, status), 0), {**scope, **labels, "measurement": status}))
        self._telemetry_snapshot = tuple(observations)
        return task_ids

    def stop_claiming(self) -> None:
        """停止任务认领和恢复扫描，保留运行任务的写回能力。"""
        self._stopped.set()
        if self._recovery_thread is not None:
            self._recovery_thread.join()

    def shutdown(self) -> None:
        """等待自有线程退出；共享线程池由生命周期管理器提前等待。"""
        self.stop_claiming()
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        now = utcnow()
        with self.resources.factory() as session:
            rows = list(session.scalars(select(Task).where(Task.status == "running", Task.lease_owner == self.owner).with_for_update(skip_locked=True)))
            for task in rows:
                if task.task_type == "image_library_processing":
                    # 扫描器在图片之间响应关闭，仍在执行的操作保留租约直到返回。
                    continue
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
                self._release_slot(session, task.scope_id, task.id, owner=self.owner, claim_generation=task.claim_generation)
            session.commit()
