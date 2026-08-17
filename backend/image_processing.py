"""逐图图片处理 job、阶段状态和专用 Worker 控制面。

该模块把视觉、Agent 和单图文本 embedding 作为三个可恢复阶段保存到 PostgreSQL。
它不负责用户、订阅或支付，只在创建新的 Agent 逻辑 Task 时调用 operation policy；
叶子 Task 的 claim/heartbeat/fencing 仍复用 ``PostgresTaskService``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update

from backend.database import (
    EMBEDDING_DIMENSIONS,
    ImageProcessingAttempt,
    ImageProcessingJob,
    ImageProcessingStage,
    Meme,
    MemeVisualEmbedding,
    MemeTextEmbedding,
    SearchMigrationState,
    ScopeContext,
    Task,
    utcnow,
)
from backend.metadata import MemeContext, Provenance, SidecarMetadata
from backend.operation_policy import (
    GrantAssociation,
    GrantAssociationStore,
    OperationPolicyError,
    OperationPolicyGateway,
    Operations,
    require_allowed,
)


logger = logging.getLogger(__name__)
STAGES = ("visual", "agent", "text_embedding")
STAGE_TASK_TYPES = {"visual": "visual_embedding_generation", "agent": "meme_context_generation", "text_embedding": "text_embedding_generation"}
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
RETRYABLE_JOB_STATUSES = frozenset({"failed", "blocked", "unknown_execution"})


class ImageProcessingError(RuntimeError):
    """图片处理控制面稳定错误。"""

    def __init__(self, code: str, message: str | None = None, *, retry_at: object | None = None):
        self.code = code
        self.retry_at = retry_at
        super().__init__(message or code)


def normalize_reverse_image_policy(value: object) -> str:
    """把缺省历史策略规范化为 forbid，拒绝未知值。"""
    if value is None or value == "":
        return "forbid"
    if value not in {"forbid", "auto"}:
        raise ImageProcessingError("invalid_reverse_image_policy")
    return str(value)


def processing_config_hash(config: Mapping[str, object] | None) -> str:
    """计算服务端 Agent/视觉配置指纹，不把客户端 grant 或 prompt 纳入。"""
    value = {str(key): config[key] for key in sorted(config or {}) if key not in {"scope_id", "user_id", "grant", "session_id", "attempt"}}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ImageProcessingSnapshot:
    """对外安全返回的 job 和阶段摘要。"""

    job_id: str
    scope_id: str
    meme_id: str
    revision: int
    image_sha256: str
    reverse_image_policy: str
    status: str
    current_stage: str | None
    stages: tuple[dict[str, object], ...]
    error: dict[str, object] | None
    retry_at: object | None
    progress: float | None = None
    message: str | None = None
    created_at: object | None = None
    updated_at: object | None = None
    completed_at: object | None = None

    def as_dict(self) -> dict[str, object]:
        """返回不包含物理路径、grant 或 scope 身份的状态结构。"""
        return {
            # ``task_id`` 和 ``task_type`` 是旧任务轮询器需要的兼容字段；
            # job_id 仍是图片处理 API 的权威标识。
            "task_id": self.job_id,
            "task_type": "image_processing",
            "job_id": self.job_id,
            "submission_mode": "pipeline",
            "image_stage": None,
            "processing_job_id": self.job_id,
            "meme_id": self.meme_id,
            "revision": self.revision,
            "image_sha256": self.image_sha256,
            "reverse_image_policy": self.reverse_image_policy,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at.isoformat() if hasattr(self.created_at, "isoformat") else self.created_at,
            "updated_at": self.updated_at.isoformat() if hasattr(self.updated_at, "isoformat") else self.updated_at,
            "completed_at": self.completed_at.isoformat() if hasattr(self.completed_at, "isoformat") else self.completed_at,
            "current_stage": self.current_stage,
            "stages": [
                {
                    **dict(item),
                    "submission_mode": "pipeline",
                    "processing_job_id": self.job_id,
                }
                for item in self.stages
            ],
            "error": self.error,
            "retry_at": self.retry_at.isoformat() if hasattr(self.retry_at, "isoformat") else self.retry_at,
        }


class ImageProcessingRepository:
    """按 scope 操作图片处理 job、阶段和 attempt 的 repository。"""

    def __init__(self, resources: Any, scope_id: ScopeContext | str):
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)

    def _session(self):
        """打开短事务 session；调用方负责 commit/rollback。"""
        return self.resources.factory()

    def get(self, job_id: UUID | str, *, for_update: bool = False) -> ImageProcessingJob | None:
        """按当前 scope 读取 job，跨 scope 标识按不存在处理。"""
        with self._session() as session:
            statement = select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.id == UUID(str(job_id)))
            if for_update:
                statement = statement.with_for_update()
            return session.scalar(statement)

    def _stages(self, session: Any, job_id: UUID) -> list[ImageProcessingStage]:
        """读取固定阶段顺序。"""
        rows = list(session.scalars(select(ImageProcessingStage).where(ImageProcessingStage.scope_id == self.scope.scope_id, ImageProcessingStage.job_id == job_id)))
        order = {name: index for index, name in enumerate(STAGES)}
        return sorted(rows, key=lambda row: order.get(row.stage, len(STAGES)))

    def create_or_reuse(self, meme_id: UUID | str, image_sha256: str, *, metadata_hash: str | None = None, config: Mapping[str, object] | None = None, reverse_image_policy: object = None, explicit_retry: bool = False) -> ImageProcessingJob:
        """创建或复用逐图 job；活动配置/策略冲突会 fail-closed。"""
        policy = normalize_reverse_image_policy(reverse_image_policy)
        config_hash = processing_config_hash(config)
        meme_uuid = UUID(str(meme_id))
        if len(image_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in image_sha256):
            raise ImageProcessingError("target_changed")
        if metadata_hash is not None and (len(metadata_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in metadata_hash)):
            raise ImageProcessingError("target_changed")
        image_sha256 = image_sha256.lower()
        with self._session() as session:
            # 没有既有行时 FOR UPDATE 无法锁住目标；按 scope/图片版本加事务锁，
            # 保证并发首次提交只产生一个 revision。
            bind = session.get_bind()
            if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"mememeow:image-processing:{self.scope.scope_id}:{meme_uuid}:{image_sha256}"},
                )
            meme = session.scalar(
                select(Meme).where(
                    Meme.scope_id == self.scope.scope_id,
                    Meme.id == meme_uuid,
                )
            )
            if meme is None or meme.sha256.lower() != image_sha256.lower():
                raise ImageProcessingError("target_changed")
            rows = list(session.scalars(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.meme_id == meme_uuid, ImageProcessingJob.image_sha256 == image_sha256).order_by(ImageProcessingJob.revision.desc(), ImageProcessingJob.created_at.desc()).with_for_update()))
            active = next((row for row in rows if row.status in ACTIVE_JOB_STATUSES), None)
            if active is not None:
                if active.processing_config_hash != config_hash or active.reverse_image_policy != policy or active.metadata_hash != metadata_hash:
                    raise ImageProcessingError("generation_policy_conflict")
                session.commit()
                return active
            latest = rows[0] if rows else None
            if latest is not None and not explicit_retry and latest.processing_config_hash == config_hash and latest.reverse_image_policy == policy and latest.metadata_hash == metadata_hash:
                session.commit()
                return latest
            revision = (latest.revision + 1) if latest is not None else 1
            job = ImageProcessingJob(scope_id=self.scope.scope_id, meme_id=meme_uuid, revision=revision, image_sha256=image_sha256, metadata_hash=metadata_hash, processing_config_hash=config_hash, processing_config=dict(config or {}), reverse_image_policy=policy, status="queued", current_stage="visual")
            session.add(job)
            session.flush()
            for stage in STAGES:
                session.add(ImageProcessingStage(scope_id=self.scope.scope_id, job_id=job.id, stage=stage, status="queued"))
            session.commit()
            return job

    def snapshot(self, job_id: UUID | str) -> ImageProcessingSnapshot | None:
        """读取 job 和阶段有限诊断。"""
        with self._session() as session:
            job = session.scalar(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.id == UUID(str(job_id))))
            if job is None:
                return None
            stages = self._stages(session, job.id)
            completed_stages = sum(item.status == "succeeded" for item in stages)
            progress = completed_stages / len(STAGES) if stages else None
            message = None
            if job.error and isinstance(job.error, Mapping):
                message = str(job.error.get("message") or job.error.get("error") or "图片处理失败")
            elif job.current_stage:
                message = f"阶段：{job.current_stage}"
            return ImageProcessingSnapshot(
                str(job.id),
                self.scope.scope_id,
                str(job.meme_id),
                job.revision,
                job.image_sha256,
                normalize_reverse_image_policy(job.reverse_image_policy),
                job.status,
                job.current_stage,
                tuple({"stage": item.stage, "status": item.status, "task_id": item.task_id, "attempt": item.attempt_count, "error": item.error, "retry_at": item.retry_at} for item in stages),
                job.error,
                job.retry_at,
                progress,
                message,
                job.created_at,
                job.updated_at,
                job.completed_at,
            )

    def claim(self, job_id: UUID | str, *, owner: str, lease_seconds: int = 120) -> ImageProcessingJob | None:
        """以 owner/generation fencing 认领 queued 或过期 job。"""
        now = utcnow()
        with self._session() as session:
            statement = select(ImageProcessingJob).where(
                ImageProcessingJob.scope_id == self.scope.scope_id,
                ImageProcessingJob.id == UUID(str(job_id)),
                (ImageProcessingJob.status == "queued")
                | ((ImageProcessingJob.status == "running") & (ImageProcessingJob.lease_expires_at < now)),
            ).with_for_update(skip_locked=True)
            job = session.scalar(statement)
            if job is None:
                session.commit()
                return None
            job.status = "running"
            job.lease_owner = owner
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.claim_generation += 1
            job.updated_at = now
            session.commit()
            return job

    def transition(self, job_id: UUID | str, stage: str, *, owner: str, claim_generation: int, status: str, task_id: str | None = None, error: dict[str, object] | None = None, retry_at: object | None = None) -> bool:
        """按 job claim 更新阶段并在最后阶段完成 job，影响行数为零即 fencing 拒绝。"""
        if stage not in STAGES or status not in {"queued", "running", "succeeded", "failed", "blocked", "unknown_execution"} or not isinstance(owner, str) or not owner or not isinstance(claim_generation, int) or claim_generation < 1:
            raise ImageProcessingError("invalid_stage_transition")
        now = utcnow()
        with self._session() as session:
            job = session.scalar(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.id == UUID(str(job_id)), ImageProcessingJob.status == "running", ImageProcessingJob.lease_owner == owner, ImageProcessingJob.claim_generation == claim_generation, ImageProcessingJob.lease_expires_at > now).with_for_update())
            if job is None:
                session.commit()
                logger.info("image_processing_fencing_rejection job=%s scope=%s generation=%s", job_id, self.scope.scope_id, claim_generation)
                return False
            current = session.scalar(select(ImageProcessingStage).where(ImageProcessingStage.scope_id == self.scope.scope_id, ImageProcessingStage.job_id == job.id, ImageProcessingStage.stage == stage).with_for_update())
            if current is None:
                session.commit()
                return False
            pending_stage = next((item.stage for item in self._stages(session, job.id) if item.status != "succeeded"), None)
            if pending_stage != stage:
                # 只允许固定三阶段的第一个未完成阶段写回；旧叶子 Task
                # 不能越过前置阶段直接推进父 job。
                session.commit()
                return False
            allowed_transitions = {
                "queued": {"queued", "running", "succeeded", "failed", "blocked", "unknown_execution"},
                "running": {"running", "succeeded", "failed", "blocked", "unknown_execution"},
                "succeeded": {"succeeded"},
                "failed": {"failed"},
                "blocked": {"blocked"},
                "unknown_execution": {"unknown_execution"},
            }
            if status not in allowed_transitions.get(current.status, set()):
                session.commit()
                return False
            previous_status = current.status
            current.status = status
            current.task_id = task_id or current.task_id
            current.error = error
            current.retry_at = retry_at
            if status == "running" and previous_status != "running":
                current.attempt_count += 1
            current.updated_at = now
            job.current_stage = stage
            job.error = error
            job.retry_at = retry_at
            job.updated_at = now
            if status in {"failed", "blocked", "unknown_execution"}:
                job.status = status
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
            elif status == "succeeded" and stage == STAGES[-1]:
                job.status = "succeeded"
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
            elif status == "succeeded":
                next_stage = STAGES[STAGES.index(stage) + 1]
                next_row = session.scalar(
                    select(ImageProcessingStage).where(
                        ImageProcessingStage.scope_id == self.scope.scope_id,
                        ImageProcessingStage.job_id == job.id,
                        ImageProcessingStage.stage == next_stage,
                    ).with_for_update()
                )
                if next_row is not None and next_row.status == "queued":
                    job.current_stage = next_stage
            session.commit()
            return True

    def update_metadata_hash(self, job_id: UUID | str, *, owner: str, claim_generation: int, metadata_hash: str) -> bool:
        """在当前 job claim 内冻结 Agent 写回后的最新 metadata hash。"""
        if not isinstance(metadata_hash, str) or len(metadata_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in metadata_hash):
            return False
        now = utcnow()
        with self._session() as session:
            job = session.scalar(
                select(ImageProcessingJob)
                .where(
                    ImageProcessingJob.scope_id == self.scope.scope_id,
                    ImageProcessingJob.id == UUID(str(job_id)),
                    ImageProcessingJob.status == "running",
                    ImageProcessingJob.lease_owner == owner,
                    ImageProcessingJob.claim_generation == claim_generation,
                    ImageProcessingJob.lease_expires_at > now,
                )
                .with_for_update()
            )
            if job is None:
                session.commit()
                logger.info("image_processing_fencing_rejection job=%s scope=%s generation=%s", job_id, self.scope.scope_id, claim_generation)
                return False
            job.metadata_hash = metadata_hash
            job.updated_at = now
            session.commit()
            return True

    def retry(self, job_id: UUID | str, *, policy: object = None, config: Mapping[str, object] | None = None) -> ImageProcessingJob:
        """为 failed/blocked/unknown job 创建新 revision，旧 job 保持终态。"""
        with self._session() as session:
            old = session.scalar(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.id == UUID(str(job_id))))
            if old is None:
                raise ImageProcessingError("job_not_found")
            if old.status not in RETRYABLE_JOB_STATUSES:
                raise ImageProcessingError("job_not_retryable")
            meme_id, sha, metadata_hash = old.meme_id, old.image_sha256, old.metadata_hash
            previous_config = dict(old.processing_config or {})
            old_policy = old.reverse_image_policy
        return self.create_or_reuse(
            meme_id,
            sha,
            metadata_hash=metadata_hash,
            config=previous_config if config is None else config,
            reverse_image_policy=old_policy if policy is None else policy,
            explicit_retry=True,
        )

    def list(self, *, limit: int = 100) -> list[ImageProcessingSnapshot]:
        """分页读取当前 scope job，不返回其他 scope 的 existence。"""
        with self._session() as session:
            ids = list(session.scalars(select(ImageProcessingJob.id).where(ImageProcessingJob.scope_id == self.scope.scope_id).order_by(ImageProcessingJob.updated_at.desc(), ImageProcessingJob.id.desc()).limit(max(1, min(limit, 500)))))
        return [snapshot for identifier in ids if (snapshot := self.snapshot(identifier)) is not None]

    def latest_for_target(self, meme_id: UUID | str, image_sha256: str) -> ImageProcessingSnapshot | None:
        """读取当前 scope/目标版本的最新 job，供显式批量重试判断。"""
        try:
            identifier = UUID(str(meme_id))
        except (TypeError, ValueError):
            return None
        with self._session() as session:
            job = session.scalar(
                select(ImageProcessingJob)
                .where(
                    ImageProcessingJob.scope_id == self.scope.scope_id,
                    ImageProcessingJob.meme_id == identifier,
                    ImageProcessingJob.image_sha256 == image_sha256,
                )
                .order_by(ImageProcessingJob.revision.desc(), ImageProcessingJob.created_at.desc())
            )
            if job is None:
                return None
            job_id = job.id
        return self.snapshot(job_id)

    def attach_task(self, job_id: UUID | str, stage: str, task_id: str) -> bool:
        """把 pipeline 叶子 Task 绑定到同 scope 阶段并固化来源事实。

        只有没有来源、没有独立提交或其它 Job 关联的兼容历史 Task 才能被
        绑定。绑定同时更新 Task 专用来源列和 payload，避免后续查询依赖旧
        payload 猜测父 Job。
        """
        if stage not in STAGES or not isinstance(task_id, str) or not task_id:
            raise ImageProcessingError("invalid_stage_transition")
        with self._session() as session:
            job = session.scalar(
                select(ImageProcessingJob).where(
                    ImageProcessingJob.scope_id == self.scope.scope_id,
                    ImageProcessingJob.id == UUID(str(job_id)),
                ).with_for_update()
            )
            row = session.scalar(
                select(ImageProcessingStage)
                .where(
                    ImageProcessingStage.scope_id == self.scope.scope_id,
                    ImageProcessingStage.job_id == UUID(str(job_id)),
                    ImageProcessingStage.stage == stage,
                )
                .with_for_update()
            )
            if job is None or row is None:
                session.commit()
                return False
            child = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id))
            if child is None or child.task_type != STAGE_TASK_TYPES[stage]:
                session.commit()
                return False
            # standalone Task 和已经属于其它 Job 的 Task 均不可被 pipeline
            # 静默抢占；这也是数据库来源互斥约束之外的业务层早拒绝。
            if child.submission_mode == "standalone" or child.processing_job_id not in {None, job.id}:
                session.commit()
                return False
            if child.submission_mode not in {None, "pipeline"}:
                session.commit()
                return False
            if child.image_stage not in {None, stage}:
                session.commit()
                return False
            child_payload = dict(child.payload or {})
            if child_payload.get("submission_mode") == "standalone" or child_payload.get("job_id") not in {None, str(job.id)} or child_payload.get("meme_id") not in {None, str(job.meme_id)} or child_payload.get("image_sha256") not in {None, job.image_sha256}:
                session.commit()
                return False
            if row.task_id not in {None, task_id}:
                stale = session.scalar(
                    select(Task).where(
                        Task.scope_id == self.scope.scope_id,
                        Task.id == row.task_id,
                    )
                )
                # Worker 发现阶段关联的叶子 Task 已被清理时允许换绑；仍存在
                # 的旧任务不能被另一个任务静默抢走，避免两个父 job 共用执行事实。
                if stale is not None:
                    session.commit()
                    return False
            row.task_id = task_id
            # 标记兼容入口创建的任务归属于新控制面，避免旧 handler 再次推进下游。
            child.submission_mode = "pipeline"
            child.image_stage = stage
            child.processing_job_id = job.id
            child_payload.update({"job_id": str(job.id), "stage": stage, "submission_mode": "pipeline", "meme_id": str(job.meme_id), "image_sha256": job.image_sha256})
            child.payload = child_payload
            row.updated_at = utcnow()
            session.commit()
            return True


class ImageProcessingWorker:
    """按 job scope 调度三类叶子 Task 的有界 Worker。"""

    def __init__(self, resources: Any, *, scope_id: ScopeContext | str, task_service: Any, policy: OperationPolicyGateway | None = None, grant_store: GrantAssociationStore | None = None, owner: str | None = None, max_workers: int = 2, handlers: Mapping[str, Callable[[dict[str, object]], object]] | None = None, task_handlers: Mapping[str, Callable[..., object]] | None = None, reconcile_interval: float = 2.0):
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.tasks = task_service
        self.jobs = ImageProcessingRepository(resources, self.scope)
        self.policy = policy or OperationPolicyGateway(None)
        self.grants = grant_store or GrantAssociationStore()
        self.owner = owner or f"image-worker-{uuid4().hex}"
        self.handlers = dict(handlers or {})
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8)), thread_name_prefix="mememeow-image-worker")
        self._task_runner = None
        if callable(getattr(resources, "factory", None)):
            # 图片叶子任务使用独立 facade，但仍复用同一 PostgreSQL claim、lane
            # 和 fencing 原语；通用 manager 不会看到这些任务。
            from backend.pg_services import PostgresTaskService

            self._task_runner = PostgresTaskService(
                resources,
                scope_id=self.scope,
                agent_concurrency=int(getattr(task_service, "agent_concurrency", 1)),
                agent_backpressure=int(getattr(task_service, "agent_backpressure", 32)),
                settings_version=getattr(task_service, "settings_version", None),
                lease_seconds=int(getattr(task_service, "lease_seconds", 120)),
                max_attempts=int(getattr(task_service, "max_attempts", 3)),
                executor=self.executor,
                finalize_image_tasks=False,
                operation_policy=self.policy,
                grant_store=self.grants,
            )
            for task_type, handler in (task_handlers or {}).items():
                self._task_runner.register(task_type, handler)
        self._stopped = threading.Event()
        self._reconcile_interval = max(0.25, min(float(reconcile_interval), 60.0))
        self._reconcile_thread: threading.Thread | None = None
        self._scheduled: set[str] = set()
        self._lock = threading.RLock()

    def submit(self, meme_id: UUID | str, image_sha256: str, *, metadata_hash: str | None = None, config: Mapping[str, object] | None = None, reverse_image_policy: object = None, explicit_retry: bool = False, schedule: bool = True) -> ImageProcessingSnapshot:
        """创建/复用 job 并安排逐图处理，不等待任一叶子 Task。"""
        job = self.jobs.create_or_reuse(meme_id, image_sha256, metadata_hash=metadata_hash, config=config, reverse_image_policy=reverse_image_policy, explicit_retry=explicit_retry)
        if schedule:
            self.schedule(job.id)
        snapshot = self.jobs.snapshot(job.id)
        if snapshot is None:
            raise ImageProcessingError("job_not_found")
        return snapshot

    @staticmethod
    def _canonical_stage(stage: str) -> str:
        """把公开阶段名或任务类型收敛为视觉、Agent、文本三种内部阶段。"""
        aliases = {
            "visual": "visual",
            "visual_embedding_generation": "visual",
            "agent": "agent",
            "meme_context_generation": "agent",
            "text_embedding": "text_embedding",
            "text_embedding_generation": "text_embedding",
        }
        if not isinstance(stage, str) or aliases.get(stage) is None:
            raise ImageProcessingError("invalid_image_stage")
        return aliases[stage]

    def _fail_unclaimed_task(self, task_id: str, code: str) -> None:
        """将尚未认领的独立任务收束为稳定失败，避免 policy 拒绝留下假排队项。"""
        if not callable(getattr(self.resources, "factory", None)):
            return
        with self.resources.factory() as session:
            task = session.scalar(
                select(Task).where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == task_id,
                    Task.status == "queued",
                ).with_for_update()
            )
            if task is None:
                session.commit()
                return
            now = utcnow()
            task.status = "failed"
            task.message = "独立阶段提交被策略拒绝"
            task.error = {"error": code}
            task.completed_at = now
            task.updated_at = now
            session.commit()

    def submit_stage(
        self,
        meme_id: UUID | str,
        stage: str,
        *,
        config: Mapping[str, object] | None = None,
        reverse_image_policy: object = None,
        explicit_retry: bool = False,
        schedule: bool = True,
    ) -> Any:
        """提交或复用一个无父 Job 的独立图片阶段 Task。

        目标 SHA、阶段配置和 scope 均由当前数据库 Meme 与服务端配置派生。
        活动任务按持久 dedupe key 复用；终态任务因不再属于活动集合而由同一
        请求创建新的逻辑 Task。只有 Agent 阶段会在 Task 建立后取得 grant。
        """
        del explicit_retry  # 终态任务天然脱离活动 dedupe 集合，显式参数仅保留 API 兼容性。
        canonical = self._canonical_stage(stage)
        task_type = STAGE_TASK_TYPES[canonical]
        policy = normalize_reverse_image_policy(reverse_image_policy)
        config_value = dict(config or {})
        config_hash = processing_config_hash(config_value)
        try:
            identifier = UUID(str(meme_id))
        except (TypeError, ValueError) as exc:
            raise ImageProcessingError("target_changed") from exc
        with self.resources.factory() as session:
            meme = session.scalar(
                select(Meme).where(
                    Meme.scope_id == self.scope.scope_id,
                    Meme.id == identifier,
                )
            )
            if meme is None:
                raise ImageProcessingError("target_changed")
            image_sha256 = str(meme.sha256).lower()
            metadata_hash = self._metadata_hash(meme) if canonical == "text_embedding" else None

        payload: dict[str, object] = {
            "submission_mode": "standalone",
            "stage": canonical,
            "meme_id": str(identifier),
            "image_sha256": image_sha256,
            "processing_config_hash": config_hash,
            "reverse_image_policy": policy,
            # 该 nonce 只用于识别并发 submit 的胜者，不进入 dedupe key。
            "standalone_submission_nonce": uuid4().hex,
        }
        if metadata_hash is not None:
            payload["metadata_hash"] = metadata_hash
        safe_config_keys = {
            "agent_model",
            "skill_hash",
            "settings_version",
            "visual_model",
            "visual_dimensions",
            "preprocess_version",
            "embedding_model",
            "embedding_dimensions",
        }
        payload.update({key: value for key, value in config_value.items() if str(key) in safe_config_keys})
        submitter = self._task_runner or self.tasks
        dedupe_key = self._task_dedupe_key(task_type, payload)
        active_task_id = self._find_active_task(submitter, task_type, dedupe_key)
        if active_task_id is not None:
            existing = submitter.get(active_task_id) if callable(getattr(submitter, "get", None)) else None
            if existing is not None:
                return existing

        try:
            record = submitter.submit(task_type, payload, schedule=False)
        except Exception as exc:
            raise exc
        task_id = self._task_identifier(record)
        if task_id is None:
            raise ImageProcessingError("stage_task_create_failed")
        returned_payload = getattr(record, "payload", None)
        # TaskRepository 的活动唯一键是最终裁判；竞争请求拿到已有 Task 时不应
        # 再 acquire 一个 Agent grant。
        if isinstance(returned_payload, Mapping) and returned_payload.get("standalone_submission_nonce") != payload["standalone_submission_nonce"]:
            if schedule and callable(getattr(submitter, "schedule", None)):
                submitter.schedule(task_id)
            return record

        if canonical == "agent":
            grant_key = f"standalone-agent:{identifier}:{image_sha256}:{config_hash}:{policy}:{payload['standalone_submission_nonce']}"
            payload["agent_grant_key"] = grant_key
            association: GrantAssociation | None = None
            request = self.policy.request(self.scope, Operations.ANALYSIS_AGENT, grant_key, resource_id=str(identifier), task_id=task_id, source="image-processing-standalone")
            try:
                association = self.grants.get(request)
                if association is None:
                    association = self.grants.acquire(request, self.policy)
                if association.state in {"unknown", "released"}:
                    raise OperationPolicyError("operation_grant_invalid")
                if callable(getattr(self.grants, "bind_task", None)) and not self.grants.bind_task(association.grant, task_id):
                    raise OperationPolicyError("operation_grant_invalid")
            except OperationPolicyError as exc:
                self._fail_unclaimed_task(task_id, exc.code)
                raise ImageProcessingError(exc.code, retry_at=exc.retry_at) from exc
            # 任务已创建后再把不透明 grant key 写入服务端 payload；key 不是 grant
            # 凭据，handler 仍会从服务端 grant store 取回真正授权。
            if callable(getattr(self.resources, "factory", None)):
                with self.resources.factory() as session:
                    task = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
                    if task is not None:
                        next_payload = dict(task.payload or {})
                        next_payload["agent_grant_key"] = grant_key
                        task.payload = next_payload
                        session.commit()

        if schedule and callable(getattr(submitter, "schedule", None)):
            submitter.schedule(task_id)
        refreshed = submitter.get(task_id) if callable(getattr(submitter, "get", None)) else None
        return refreshed or record

    # 兼容控制面和集成测试使用的更明确命名。
    submit_standalone = submit_stage
    submit_independent_stage = submit_stage

    def schedule(self, job_id: UUID | str) -> None:
        """加入有界线程池，重复 schedule 不创建第二个执行。"""
        identifier = str(job_id)
        with self._lock:
            if self._stopped.is_set() or identifier in self._scheduled:
                return
            self._scheduled.add(identifier)
        self.executor.submit(self._run, identifier)

    def _mark_grant_unknown(self, association: GrantAssociation | None) -> None:
        """将无法确认收束的 Agent grant 标记为 unknown，禁止后续盲目重放。"""
        if association is None or not callable(getattr(self.grants, "transition", None)):
            return
        try:
            self.grants.transition(association.grant, "unknown")
        except OperationPolicyError:
            logger.warning("image_processing_grant_transition_failed operation=%s", association.grant.operation)

    @staticmethod
    def _metadata_hash(meme: Meme) -> str | None:
        """按元数据服务相同的规范化模型计算当前 Meme metadata hash。"""
        try:
            payload: dict[str, object] = {
                "schema_version": meme.metadata_schema_version,
                "image": {
                    "relative_path": meme.storage_key,
                    "extension": meme.extension,
                    "size_bytes": meme.size_bytes,
                    "sha256": meme.sha256,
                },
                "context_status": meme.context_status,
                "meme_context": MemeContext.model_validate(meme.meme_context or {}).model_dump(mode="json", exclude_none=False),
                "provenance": Provenance.model_validate(meme.provenance or {}).model_dump(mode="json", exclude_none=False),
            }
            payload.update(meme.extensions or {})
            metadata = SidecarMetadata.model_validate(payload)
            serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:  # noqa: BLE001 - 非法历史元数据只表示阶段尚未有效
            return None
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _stage_valid(self, job: ImageProcessingJob, stage: str) -> bool:
        """重新校验目标和阶段产物，返回当前阶段是否可以安全复用。"""
        if stage not in STAGES:
            raise ImageProcessingError("invalid_stage_transition")
        with self.resources.factory() as session:
            meme = session.scalar(
                select(Meme).where(
                    Meme.scope_id == self.scope.scope_id,
                    Meme.id == job.meme_id,
                )
            )
            if meme is None or meme.sha256.lower() != job.image_sha256.lower():
                raise ImageProcessingError("target_changed")

            config = dict(job.processing_config or {})
            if stage == "visual":
                model = config.get("visual_model")
                preprocess = config.get("preprocess_version")
                dimensions = config.get("visual_dimensions")
                try:
                    dimensions = int(dimensions)
                except (TypeError, ValueError):
                    return False
                if not isinstance(model, str) or not model or not isinstance(preprocess, str) or not preprocess:
                    return False
                row = session.scalar(
                    select(MemeVisualEmbedding).where(
                        MemeVisualEmbedding.scope_id == self.scope.scope_id,
                        MemeVisualEmbedding.meme_id == meme.id,
                        MemeVisualEmbedding.model == model,
                        MemeVisualEmbedding.preprocess_version == preprocess,
                        MemeVisualEmbedding.dimensions == dimensions,
                        MemeVisualEmbedding.image_sha256 == meme.sha256,
                    )
                )
                return row is not None and row.embedding is not None

            if stage == "agent":
                if meme.context_status != "ready":
                    return False
                summary = (meme.provenance or {}).get("agent_context")
                if not isinstance(summary, Mapping):
                    return False
                if summary.get("image_sha256") != meme.sha256:
                    return False
                if summary.get("model") != config.get("agent_model"):
                    return False
                if summary.get("reverse_image_policy") != normalize_reverse_image_policy(job.reverse_image_policy):
                    return False
                if summary.get("processing_config_hash") != job.processing_config_hash:
                    return False
                if "skill_hash" in config and summary.get("skill_hash") != config.get("skill_hash"):
                    return False
                return bool(summary.get("task_id") and summary.get("completed_at"))

            current_metadata_hash = self._metadata_hash(meme)
            if current_metadata_hash is None:
                return False
            if job.metadata_hash is not None and job.metadata_hash != current_metadata_hash:
                return False
            expected_metadata_hash = job.metadata_hash or current_metadata_hash
            model = config.get("embedding_model")
            if not isinstance(model, str) or not model:
                return False
            row = session.scalar(
                select(MemeTextEmbedding).where(
                    MemeTextEmbedding.scope_id == self.scope.scope_id,
                    MemeTextEmbedding.meme_id == meme.id,
                    MemeTextEmbedding.image_sha256 == meme.sha256,
                    MemeTextEmbedding.metadata_hash == expected_metadata_hash,
                    MemeTextEmbedding.embedding_model_version == model,
                    MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                    MemeTextEmbedding.status == "ready",
                    MemeTextEmbedding.embedding.is_not(None),
                )
            )
            return row is not None

    def _run(self, job_id: str) -> None:
        """认领 job，按阶段顺序创建或执行一个叶子 Task。"""
        reschedule = False
        try:
            current_job = self.jobs.get(job_id)
            now = utcnow()
            if current_job is not None and current_job.status == "running" and current_job.lease_owner == self.owner and current_job.lease_expires_at is not None and current_job.lease_expires_at > now:
                job = current_job
            else:
                job = self.jobs.claim(job_id, owner=self.owner)
            if job is None:
                return
            stages = self.jobs.snapshot(job.id)
            if stages is None:
                return
            stage = next((item for item in stages.stages if item["status"] != "succeeded"), None)
            if stage is None:
                return
            name = str(stage["stage"])
            task_id = stage.get("task_id")

            try:
                valid = self._stage_valid(job, name)
            except ImageProcessingError as exc:
                self.jobs.transition(
                    job.id,
                    name,
                    owner=self.owner,
                    claim_generation=job.claim_generation,
                    status="failed",
                    task_id=str(task_id) if task_id else None,
                    error={"error": exc.code},
                )
                return
            if valid:
                if name == "agent":
                    self._refresh_metadata_hash(job)
                if stage["status"] != "succeeded":
                    if not self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="succeeded", task_id=str(task_id) if task_id else None):
                        return
                if name != STAGES[-1]:
                    reschedule = True
                return

            if not task_id:
                try:
                    task_id = self._prepare_task(job, name)
                except ImageProcessingError as exc:
                    # policy blocked 通常已由 _prepare_task 写入阶段；其它创建
                    # 失败必须释放 job claim，不能留下永远 running 的父记录。
                    if exc.code != "blocked":
                        terminal_status = "unknown_execution" if exc.code == "unknown_execution" else "failed"
                        self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, error={"error": exc.code}, retry_at=exc.retry_at)
                    return
                except Exception as exc:  # noqa: BLE001 - 创建失败只记录稳定诊断
                    self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="failed", error={"error": "stage_task_create_failed"})
                    logger.info("image_processing_task_create_failed job=%s stage=%s error=%s", job.id, name, type(exc).__name__)
                    return
            child = self.tasks.get(str(task_id)) if callable(getattr(self.tasks, "get", None)) else None
            if task_id and child is None:
                # 阶段关联的叶子任务可能被人工清理；缺失任务不能永久阻塞父 job，
                # 由当前 job claim 重新创建同一阶段的叶子任务。
                try:
                    task_id = self._prepare_task(job, name)
                except ImageProcessingError as exc:
                    if exc.code != "blocked":
                        terminal_status = "unknown_execution" if exc.code == "unknown_execution" else "failed"
                        self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, error={"error": exc.code}, retry_at=exc.retry_at)
                    return
                except Exception as exc:  # noqa: BLE001 - 创建失败只记录稳定诊断
                    self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="failed", error={"error": "stage_task_create_failed"})
                    logger.info("image_processing_task_create_failed job=%s stage=%s error=%s", job.id, name, type(exc).__name__)
                    return
                child = self.tasks.get(str(task_id)) if callable(getattr(self.tasks, "get", None)) else None
            if child is not None and child.status == "succeeded":
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="succeeded", task_id=str(task_id))
                if name == "agent":
                    self._refresh_metadata_hash(job)
                if name != STAGES[-1]:
                    reschedule = True
                return
            if child is not None and child.status == "failed":
                child_error = child.error if isinstance(child.error, dict) else {"error": "stage_failed"}
                stage_error = str(child_error.get("error") or "stage_failed")
                terminal_status = "unknown_execution" if stage_error in {"unknown_execution", "reverse_image_unknown_execution"} else "failed"
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, task_id=str(task_id), error={"error": stage_error})
                return
            if self._task_runner is not None:
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="running", task_id=str(task_id))
                self._task_runner.schedule(str(task_id))
                return
            handler = self.handlers.get(name)
            if handler is None:
                # 叶子 Task 由共享任务 Worker 执行；此处只持久化可信关联。
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="running", task_id=str(task_id))
                return
            self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="running", task_id=str(task_id))
            try:
                handler({"job_id": str(job.id), "task_id": str(task_id), "stage": name, "scope_id": self.scope.scope_id, "image_sha256": job.image_sha256, "reverse_image_policy": normalize_reverse_image_policy(job.reverse_image_policy)})
            except ImageProcessingError as exc:
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=exc.code if exc.code in {"blocked", "unknown_execution"} else "failed", task_id=str(task_id), error={"error": exc.code}, retry_at=exc.retry_at)
            except Exception as exc:  # noqa: BLE001 - 叶子错误不泄露原文
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="failed", task_id=str(task_id), error={"error": "stage_failed"})
                logger.info("image_processing_stage_failed job=%s stage=%s error=%s", job.id, name, type(exc).__name__)
            else:
                if name == "agent":
                    self._refresh_metadata_hash(job)
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status="succeeded", task_id=str(task_id))
                if name != STAGES[-1]:
                    reschedule = True
        finally:
            with self._lock:
                self._scheduled.discard(str(job_id))
            if reschedule and not self._stopped.is_set():
                self.schedule(job_id)

    def _refresh_metadata_hash(self, job: ImageProcessingJob) -> None:
        """把当前 Meme 语境指纹写回 job，避免 Agent 成功后沿用旧 hash。"""
        with self.resources.factory() as session:
            meme = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == job.meme_id))
        current = self._metadata_hash(meme) if meme is not None else None
        if current is not None and current != job.metadata_hash:
            self.jobs.update_metadata_hash(job.id, owner=self.owner, claim_generation=job.claim_generation, metadata_hash=current)

    @staticmethod
    def _task_dedupe_key(task_type: str, payload: Mapping[str, object]) -> str:
        """按 PostgreSQL 任务 facade 的规则生成活动叶子 Task 去重键。

        提交来源参与 key，避免同一图片阶段的 Job 子任务和独立任务互相复用。
        """
        mode = payload.get("submission_mode") or ("pipeline" if payload.get("job_id") else "legacy")
        stage = payload.get("stage") or {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "text_embedding_generation": "text_embedding",
        }.get(task_type)
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
        return f"{task_type}:{json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    @staticmethod
    def _task_identifier(record: object) -> str | None:
        """从 PostgreSQL ORM 或兼容 TaskRecord 中读取任务 ID。"""
        value = getattr(record, "task_id", None) or getattr(record, "id", None)
        return value if isinstance(value, str) and value else None

    def _find_active_task(self, submitter: Any, task_type: str, dedupe_key: str) -> str | None:
        """在 policy acquire 前查询当前 scope 的活动叶子 Task。"""
        finder = getattr(submitter, "find_active", None)
        record = finder(task_type, dedupe_key) if callable(finder) else None
        if record is not None and getattr(record, "status", None) in {"queued", "running"}:
            return self._task_identifier(record)
        if callable(finder):
            return None

        # 兼容尚未实现 find_active 的任务 facade；结果只用于减少无效 acquire，
        # 提交阶段仍由持久 dedupe 唯一键作最终裁决。
        lister = getattr(submitter, "list", None)
        if not callable(lister):
            return None
        cursor = None
        while True:
            try:
                records, cursor = lister(statuses={"queued", "running"}, task_types={task_type}, cursor=cursor, limit=100)
            except TypeError:
                records, cursor = lister(statuses={"queued", "running"}, cursor=cursor, limit=100)
            for item in records:
                if getattr(item, "task_type", None) != task_type or getattr(item, "status", None) not in {"queued", "running"}:
                    continue
                payload = getattr(item, "payload", None)
                if isinstance(payload, Mapping) and self._task_dedupe_key(task_type, payload) == dedupe_key:
                    return self._task_identifier(item)
            if cursor is None:
                return None

    def _prepare_task(self, job: ImageProcessingJob, stage: str) -> str:
        """创建/复用唯一叶子 Task；Agent acquire 发生在 Task dedupe 后。"""
        if stage not in STAGE_TASK_TYPES:
            raise ImageProcessingError("invalid_stage_transition")
        task_type = STAGE_TASK_TYPES[stage]
        policy = normalize_reverse_image_policy(job.reverse_image_policy)
        payload: dict[str, object] = {
            "job_id": str(job.id),
            "job_revision": job.revision,
            "submission_mode": "pipeline",
            "meme_id": str(job.meme_id),
            "image_sha256": job.image_sha256,
            "reverse_image_policy": policy,
            "processing_config_hash": job.processing_config_hash,
            "stage": stage,
        }
        if stage == "text_embedding" and job.metadata_hash:
            payload["metadata_hash"] = job.metadata_hash
        for key, value in dict(job.processing_config or {}).items():
            if key not in {
                "scope",
                "scope_id",
                "user_id",
                "grant",
                "resource_id",
                "task_id",
                "job_id",
                "stage",
                "meme_id",
                "image_sha256",
                "reverse_image_policy",
                "processing_config_hash",
                "session_id",
                "attempt",
            }:
                payload.setdefault(str(key), value)
        submitter = self._task_runner or self.tasks
        dedupe_key = self._task_dedupe_key(task_type, payload)
        active_task_id = self._find_active_task(submitter, task_type, dedupe_key)
        if active_task_id is not None:
            if not self.jobs.attach_task(job.id, stage, active_task_id):
                raise ImageProcessingError("stage_task_bind_failed")
            return active_task_id

        association: GrantAssociation | None = None
        gateway_request = None
        if stage == "agent":
            logical_key = f"agent:{job.meme_id}:{job.image_sha256}:{job.processing_config_hash}:{policy}:r{job.revision}"
            # 任务尚未提交时不能把 job UUID 写入 operation_grants.task_id 外键；
            # 先取得按 logical key 去重的 grant，提交后再绑定真实 Task ID。
            gateway_request = self.policy.request(self.scope, Operations.ANALYSIS_AGENT, logical_key, resource_id=str(job.meme_id), source="image-processing")
            association = self.grants.get(gateway_request)
            if association is None:
                try:
                    if hasattr(self.grants, "acquire"):
                        association = self.grants.acquire(gateway_request, self.policy)
                    else:
                        grant = require_allowed(self.policy.acquire(gateway_request))
                        association = self.grants.put(GrantAssociation(gateway_request, grant))
                except OperationPolicyError as exc:
                    self.jobs.transition(job.id, stage, owner=self.owner, claim_generation=job.claim_generation, status="blocked", error={"error": exc.code}, retry_at=exc.retry_at)
                    raise ImageProcessingError("blocked", retry_at=exc.retry_at) from exc
            if association is not None and association.state == "unknown":
                raise ImageProcessingError("unknown_execution")
            if association is not None and association.state == "released":
                raise ImageProcessingError("operation_grant_invalid")
            # grant 只保存在服务端 association，绝不进入普通 Task payload。
        try:
            record = submitter.submit(task_type, payload, schedule=False)
        except Exception:
            if association is not None and association.state == "acquired":
                try:
                    result = self.policy.release(association.grant)
                    if not result.ok or result.state not in {"released", "already_released"}:
                        self._mark_grant_unknown(association)
                    elif callable(getattr(self.grants, "transition", None)) and not self.grants.transition(association.grant, "released"):
                        self._mark_grant_unknown(association)
                except OperationPolicyError:
                    self._mark_grant_unknown(association)
            raise
        task_id = self._task_identifier(record)
        if task_id is None:
            self._mark_grant_unknown(association)
            raise ImageProcessingError("stage_task_create_failed")
        if association is not None and callable(getattr(self.grants, "bind_task", None)):
            if not self.grants.bind_task(association.grant, task_id):
                # Task 已经持久化，不能再释放 grant；让父 job 记录稳定失败，等待
                # 人工重试或恢复流程处理这条不完整关联。
                self._mark_grant_unknown(association)
                raise ImageProcessingError("stage_grant_bind_failed")
        if not self.jobs.attach_task(job.id, stage, task_id):
            self._mark_grant_unknown(association)
            raise ImageProcessingError("stage_task_bind_failed")
        return task_id

    def reconcile(self, *, limit: int = 100) -> int:
        """扫描当前 scope 未完成 job，逐图安排恢复。"""
        count = 0
        for snapshot in self.jobs.list(limit=limit):
            if snapshot.status in {"queued", "running"}:
                self.schedule(snapshot.job_id)
                count += 1
        return count

    def start(self) -> int:
        """启动恢复扫描；未把全库图片 ID 放入单一任务 payload。"""
        self._stopped.clear()
        if self._task_runner is not None:
            self._task_runner.start()
        count = self.reconcile()
        if self._reconcile_thread is None or not self._reconcile_thread.is_alive():
            self._reconcile_thread = threading.Thread(target=self._reconcile_loop, name=f"{self.owner}-reconcile", daemon=True)
            self._reconcile_thread.start()
        return count

    def _reconcile_loop(self) -> None:
        """以低频有界扫描推进已完成叶子 Task，避免请求线程承担全库协调。"""
        while not self._stopped.wait(self._reconcile_interval):
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001 - 恢复扫描不能终止应用
                logger.info("image_processing_reconcile_failed scope=%s error=%s", self.scope.scope_id, type(exc).__name__)

    def shutdown(self) -> None:
        """停止新 job 认领并释放线程池，不修改其他 Worker 的 claim。"""
        self._stopped.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=2)
        if self._task_runner is not None:
            self._task_runner.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)


class SingleImageEmbeddingService:
    """逐图生成文本向量并按内容/语境指纹 CAS 写回。"""

    def __init__(self, resources: Any, *, scope_id: ScopeContext | str, model: str, dimensions: int = 1024, embedder: Callable[[str], Iterable[float]] | None = None):
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.model = model
        if int(dimensions) != EMBEDDING_DIMENSIONS:
            raise ImageProcessingError("embedding_dimensions_mismatch")
        self.dimensions = EMBEDDING_DIMENSIONS
        self.embedder = embedder

    def upsert(self, meme_id: UUID | str, *, image_sha256: str, metadata_hash: str, semantic_document: str) -> MemeTextEmbedding:
        """生成并原子提交单图向量；旧指纹不会覆盖新内容。"""
        if not semantic_document.strip():
            raise ImageProcessingError("query_embedding_not_ready")
        if self.embedder is None:
            raise ImageProcessingError("embedding_not_configured")
        vector = [float(value) for value in self.embedder(semantic_document)]
        if len(vector) != self.dimensions:
            raise ImageProcessingError("embedding_dimensions_mismatch")
        if not all(math.isfinite(value) for value in vector):
            raise ImageProcessingError("embedding_non_finite")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ImageProcessingError("embedding_zero_norm")
        with self.resources.factory() as session:
            meme = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if meme is None or meme.sha256 != image_sha256:
                raise ImageProcessingError("target_changed")
            current_metadata_hash = ImageProcessingWorker._metadata_hash(meme)
            if current_metadata_hash is None or current_metadata_hash != metadata_hash:
                raise ImageProcessingError("target_changed")
            statement = select(MemeTextEmbedding).where(MemeTextEmbedding.scope_id == self.scope.scope_id, MemeTextEmbedding.meme_id == meme.id, MemeTextEmbedding.image_sha256 == image_sha256, MemeTextEmbedding.metadata_hash == metadata_hash, MemeTextEmbedding.embedding_model_version == self.model).with_for_update()
            row = session.scalar(statement)
            if row is None:
                row = MemeTextEmbedding(scope_id=self.scope.scope_id, meme_id=meme.id, image_sha256=image_sha256, metadata_hash=metadata_hash, embedding_model_version=self.model, dimensions=self.dimensions, semantic_document=semantic_document[:6000], embedding=vector, status="ready")
                session.add(row)
            else:
                row.embedding = vector
                row.semantic_document = semantic_document[:6000]
                row.status = "ready"
                row.updated_at = utcnow()
            session.commit()
            return row

    def query(self, vector: Iterable[float], *, limit: int = 5) -> list[str]:
        """只查询当前 scope 且仍匹配 Meme 当前 SHA 的 ready 向量。"""
        values = [float(item) for item in vector]
        if len(values) != self.dimensions:
            raise ImageProcessingError("embedding_dimensions_mismatch")
        with self.resources.factory() as session:
            rows = list(
                session.execute(
                    select(MemeTextEmbedding, Meme)
                    .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
                    .where(
                        MemeTextEmbedding.scope_id == self.scope.scope_id,
                        MemeTextEmbedding.embedding_model_version == self.model,
                        MemeTextEmbedding.dimensions == self.dimensions,
                        MemeTextEmbedding.status == "ready",
                        MemeTextEmbedding.embedding.is_not(None),
                        Meme.sha256 == MemeTextEmbedding.image_sha256,
                    )
                    .order_by(MemeTextEmbedding.updated_at.desc(), MemeTextEmbedding.meme_id.asc())
                    .limit(500)
                )
            )
            scored: list[tuple[float, str]] = []
            norm = sum(value * value for value in values) ** 0.5
            if not math.isfinite(norm) or norm <= 0:
                raise ImageProcessingError("embedding_zero_norm")
            for row, meme in rows:
                # 语境 hash 与图片 SHA 都必须仍匹配当前 Meme，历史向量不能因模型相似而混入结果。
                if ImageProcessingWorker._metadata_hash(meme) != row.metadata_hash:
                    continue
                try:
                    candidate = [float(item) for item in row.embedding or []]
                    if len(candidate) != self.dimensions:
                        continue
                    candidate_norm = sum(item * item for item in candidate) ** 0.5
                    if not math.isfinite(candidate_norm) or candidate_norm <= 0 or not all(math.isfinite(item) for item in candidate):
                        continue
                    score = sum(left * right for left, right in zip(values, candidate)) / (norm * candidate_norm)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                scored.append((score, str(row.meme_id)))
            scored.sort(key=lambda item: (-item[0], item[1]))
            # 同一 Meme 可能保留多个历史 metadata hash；只返回一次最新相关结果。
            return list(dict.fromkeys(identifier for _score, identifier in scored))[: max(1, min(limit, 100))]


def seed_jobs(repository: ImageProcessingRepository, memes: Iterable[object], *, config: Mapping[str, object] | None = None, reverse_image_policy: object = None, page_size: int = 100) -> int:
    """分页 seed 现有图片 job，避免把全库 ID 放入单一 payload。"""
    count = 0
    page: list[object] = []
    for meme in memes:
        page.append(meme)
        if len(page) >= max(1, min(page_size, 500)):
            count += _seed_page(repository, page, config=config, reverse_image_policy=reverse_image_policy)
            page.clear()
    if page:
        count += _seed_page(repository, page, config=config, reverse_image_policy=reverse_image_policy)
    return count


def _seed_page(repository: ImageProcessingRepository, values: Iterable[object], *, config: Mapping[str, object] | None, reverse_image_policy: object) -> int:
    """提交一页 seed，并忽略单个坏记录而继续其余图片。"""
    count = 0
    for meme in values:
        try:
            snapshot = repository.create_or_reuse(getattr(meme, "id"), str(getattr(meme, "sha256")), metadata_hash=None, config=config, reverse_image_policy=reverse_image_policy)
            count += 1 if snapshot is not None else 0
        except (ImageProcessingError, ValueError, TypeError):
            continue
    return count


def stable_input_digest(*values: object) -> str:
    """生成外部 attempt 输入摘要。"""
    return hashlib.sha256("|".join(str(value) for value in values).encode()).hexdigest()
