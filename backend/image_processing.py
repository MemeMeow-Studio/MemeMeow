"""逐图图片处理 job、阶段状态和专用 Worker 控制面。

该模块把视觉、Agent、可选自动重命名和单图文本 embedding 作为四个可恢复阶段保存到 PostgreSQL。
它不负责配额或计费，只在创建新的 Agent 逻辑 Task 时调用 operation policy；
叶子 Task 的 claim/heartbeat/fencing 仍复用 ``PostgresTaskService``。
"""

from __future__ import annotations

import hashlib
import inspect
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
from backend.config import validate_agent_concurrency
from backend.image_stage_plan import (
    IMAGE_STAGE_ORDER,
    SETTLED_STAGE_STATUSES,
    STAGE_TASK_TYPES,
    ImageStagePlan,
    normalize_stage,
)
from backend.metadata import MemeContext, Provenance, SidecarMetadata
from backend.agent_resume import normalize_identifier
from backend.operation_policy import (
    GrantAssociation,
    GrantAssociationStore,
    OperationPolicyError,
    OperationPolicyGateway,
    Operations,
    require_allowed,
)
from backend.public_dto import (
    PUBLIC_STAGE_NAMES,
    PUBLIC_TASK_STATUSES,
    normalize_public_digest,
    normalize_public_identifier,
    public_processing_stage,
    public_processing_warning,
    sanitize_public_error,
    sanitize_public_message,
    sanitize_public_timestamp,
)


logger = logging.getLogger(__name__)
# 固定阶段计划由无状态模块维护；本 facade 保留历史常量名称供旧调用方使用。
STAGES = IMAGE_STAGE_ORDER
STAGE_SETTLED = SETTLED_STAGE_STATUSES
# 这些自动命名错误只表示候选名称不可用，叶子 Task 仍失败但父 Job 可以继续。
AUTO_RENAME_WARNING_ERRORS = frozenset({
    "auto_rename_title_missing",
    "auto_rename_invalid_filename",
    "auto_rename_target_exists",
    "auto_rename_target_changed",
})
AUTO_RENAME_UNKNOWN_ERRORS = frozenset({"auto_rename_unknown_execution"})
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
    if value is None:
        return "forbid"
    # 先收窄为字符串，避免 list/dict 等不可哈希输入泄漏裸 TypeError。
    if not isinstance(value, str) or value not in {"forbid", "auto"}:
        raise ImageProcessingError("invalid_reverse_image_policy")
    return value


def normalize_auto_name(value: object) -> bool:
    """严格规范化自动命名选项，缺省值安全地关闭自动命名。"""
    if value is None:
        return False
    if type(value) is not bool:
        raise ImageProcessingError("invalid_auto_name")
    return value


def image_file_matches(resources: Any, scope_id: ScopeContext | str, meme: Meme) -> bool:
    """复核当前 scope 文件的实际大小和 SHA 是否仍匹配 Meme 记录。

    该判定同时服务于成功 Job 复用和 scope 级未就绪枚举；数据库中的 SHA
    只是声明事实，不能替代对文件系统当前字节的验证。
    """
    try:
        resolver = getattr(resources, "blob_store_for_scope", None)
        blob_store = resolver(scope_id) if callable(resolver) else getattr(resources, "blob_store", None)
        if blob_store is None:
            return False
        return bool(
            blob_store.exists_with_identity(
                meme.storage_key,
                sha256=meme.sha256,
                size_bytes=meme.size_bytes,
            )
        )
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        return False


@dataclass(frozen=True)
class ImageProcessingOptions:
    """一次图片处理提交冻结的联网策略和自动命名选择。"""

    reverse_image_policy: str = "forbid"
    auto_name: bool = False

    @classmethod
    def normalize(cls, value: Mapping[str, object] | None = None, **kwargs: object) -> "ImageProcessingOptions":
        """从请求或内部映射构造严格、可序列化的处理选项。"""
        source = dict(value or {})
        source.update(kwargs)
        return cls(
            normalize_reverse_image_policy(source.get("reverse_image_policy")),
            normalize_auto_name(source.get("auto_name")),
        )


def processing_config_hash(config: Mapping[str, object] | None) -> str:
    """计算服务端 Agent/视觉配置指纹，不把客户端 grant 或 prompt 纳入。"""
    value = {str(key): config[key] for key in sorted(config or {}) if key not in {"scope_id", "user_id", "grant", "session_id", "attempt", "auto_name"}}
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
    auto_name: bool = False
    has_warnings: bool = False
    warnings: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        """返回不包含物理路径、grant 或 scope 身份的状态结构。"""
        job_id = normalize_public_identifier(self.job_id) or "invalid-job"
        meme_id = normalize_public_identifier(self.meme_id) or "invalid-meme"
        revision = self.revision if isinstance(self.revision, int) and not isinstance(self.revision, bool) and 0 < self.revision <= 1_000_000 else None
        image_sha256 = normalize_public_digest(self.image_sha256)
        reverse_image_policy = self.reverse_image_policy if isinstance(self.reverse_image_policy, str) and self.reverse_image_policy in {"forbid", "auto"} else "forbid"
        status = self.status if isinstance(self.status, str) and self.status in PUBLIC_TASK_STATUSES else "failed"
        current_stage = self.current_stage if isinstance(self.current_stage, str) and self.current_stage in PUBLIC_STAGE_NAMES else None
        progress = self.progress if isinstance(self.progress, (int, float)) and not isinstance(self.progress, bool) and 0 <= self.progress <= 1 else None
        stages = [public_processing_stage(item, job_id=job_id) for item in self.stages if isinstance(item, Mapping)]
        warnings = [public_processing_warning(item) for item in self.warnings if isinstance(item, Mapping)]
        return {
            # ``task_id`` 和 ``task_type`` 是旧任务轮询器需要的兼容字段；
            # job_id 仍是图片处理 API 的权威标识。
            "task_id": job_id,
            "task_type": "image_processing",
            "job_id": job_id,
            "submission_mode": "pipeline",
            "image_stage": None,
            "processing_job_id": job_id,
            "meme_id": meme_id,
            "revision": revision,
            "image_sha256": image_sha256,
            "reverse_image_policy": reverse_image_policy,
            "auto_name": self.auto_name if isinstance(self.auto_name, bool) else False,
            "status": status,
            "has_warnings": bool(warnings) or self.has_warnings is True,
            "warnings": warnings,
            "progress": progress,
            "message": sanitize_public_message(self.message),
            "created_at": sanitize_public_timestamp(self.created_at),
            "updated_at": sanitize_public_timestamp(self.updated_at),
            "completed_at": sanitize_public_timestamp(self.completed_at),
            "current_stage": current_stage,
            "stages": stages,
            "error": sanitize_public_error(self.error, fallback="image_processing_failed"),
            "retry_at": sanitize_public_timestamp(self.retry_at),
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

    def create_or_reuse(self, meme_id: UUID | str, image_sha256: str, *, metadata_hash: str | None = None, config: Mapping[str, object] | None = None, reverse_image_policy: object = None, auto_name: object = None, explicit_retry: bool = False) -> ImageProcessingJob:
        """创建或复用逐图 job；活动选项冲突会 fail-closed。"""
        policy = normalize_reverse_image_policy(reverse_image_policy)
        auto_name_value = normalize_auto_name(auto_name)
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
                # 既有策略契约优先于新增自动命名选项：两者同时冲突时仍返回
                # generation_policy_conflict，只有策略/配置一致且 auto_name 单独
                # 不一致时才使用更具体的 processing_options_conflict。
                if active.processing_config_hash != config_hash or active.reverse_image_policy != policy or active.metadata_hash != metadata_hash:
                    raise ImageProcessingError("generation_policy_conflict")
                if bool(getattr(active, "auto_name", False)) != auto_name_value:
                    raise ImageProcessingError("processing_options_conflict")
                session.commit()
                return active
            latest = rows[0] if rows else None
            if latest is not None and not explicit_retry and latest.processing_config_hash == config_hash and latest.reverse_image_policy == policy and bool(getattr(latest, "auto_name", False)) == auto_name_value and latest.metadata_hash == metadata_hash:
                # 只有核心产物仍然有效时才复用成功 revision；失败或已失效
                # revision 由后续分支创建新 revision，避免把过期状态当成就绪。
                if latest.status == "succeeded" and self._core_ready(session, latest):
                    session.commit()
                    return latest
            revision = (latest.revision + 1) if latest is not None else 1
            job = ImageProcessingJob(scope_id=self.scope.scope_id, meme_id=meme_uuid, revision=revision, image_sha256=image_sha256, metadata_hash=metadata_hash, processing_config_hash=config_hash, processing_config=dict(config or {}), reverse_image_policy=policy, auto_name=auto_name_value, status="queued", current_stage="visual")
            session.add(job)
            session.flush()
            for stage in STAGES:
                session.add(ImageProcessingStage(scope_id=self.scope.scope_id, job_id=job.id, stage=stage, status="skipped" if stage == "auto_rename" and not auto_name_value else "queued"))
            session.commit()
            return job

    def _core_ready(self, session: Any, job: ImageProcessingJob) -> bool:
        """验证成功 Job 的三个核心产物仍绑定当前图片和服务端配置。"""
        try:
            meme = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == job.meme_id))
            if meme is None or meme.sha256.lower() != job.image_sha256.lower():
                return False
            if not image_file_matches(self.resources, self.scope, meme):
                return False
            stage_statuses = {item.stage: item.status for item in self._stages(session, job.id)}
            if any(stage_statuses.get(stage) != "succeeded" for stage in ("visual", "agent", "text_embedding")):
                return False
            # 新 Job 的自动重命名可以跳过或以 warning 收束；其它非收束状态
            # 说明控制面尚未证明该 revision 可复用，不能只看三个核心阶段。
            auto_rename_stage = next(
                (item for item in self._stages(session, job.id) if item.stage == "auto_rename"),
                None,
            )
            auto_rename_status = auto_rename_stage.status if auto_rename_stage is not None else "skipped"
            if auto_rename_status not in STAGE_SETTLED:
                return False
            if auto_rename_status == "warning":
                warning_code = (auto_rename_stage.error or {}).get("error") if isinstance(auto_rename_stage.error, Mapping) else None
                if warning_code not in AUTO_RENAME_WARNING_ERRORS:
                    return False
            config = dict(job.processing_config or {})
            visual = session.scalar(
                select(MemeVisualEmbedding).where(
                    MemeVisualEmbedding.scope_id == self.scope.scope_id,
                    MemeVisualEmbedding.meme_id == meme.id,
                    MemeVisualEmbedding.model == config.get("visual_model"),
                    MemeVisualEmbedding.preprocess_version == config.get("preprocess_version"),
                    MemeVisualEmbedding.dimensions == int(config.get("visual_dimensions", -1)),
                    MemeVisualEmbedding.image_sha256 == meme.sha256,
                )
            )
            if visual is None or visual.embedding is None or meme.context_status != "ready":
                return False
            summary = (meme.provenance or {}).get("agent_context")
            if (
                not isinstance(summary, Mapping)
                or summary.get("image_sha256") != meme.sha256
                or summary.get("model") != config.get("agent_model")
                or summary.get("reverse_image_policy") != normalize_reverse_image_policy(job.reverse_image_policy)
                or summary.get("processing_config_hash") != job.processing_config_hash
                or ("skill_hash" in config and summary.get("skill_hash") != config.get("skill_hash"))
                or not summary.get("task_id")
                or not summary.get("completed_at")
            ):
                return False
            metadata_hash = self._metadata_hash(meme)
            if metadata_hash is None or job.metadata_hash != metadata_hash:
                return False
            text_row = session.scalar(
                select(MemeTextEmbedding).where(
                    MemeTextEmbedding.scope_id == self.scope.scope_id,
                    MemeTextEmbedding.meme_id == meme.id,
                    MemeTextEmbedding.image_sha256 == meme.sha256,
                    MemeTextEmbedding.metadata_hash == metadata_hash,
                    MemeTextEmbedding.embedding_model_version == config.get("embedding_model"),
                    MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                    MemeTextEmbedding.status == "ready",
                    MemeTextEmbedding.embedding.is_not(None),
                )
            )
            return text_row is not None
        except (TypeError, ValueError, AttributeError):
            return False

    def snapshot(self, job_id: UUID | str) -> ImageProcessingSnapshot | None:
        """读取 job 和阶段有限诊断。"""
        with self._session() as session:
            job = session.scalar(select(ImageProcessingJob).where(ImageProcessingJob.scope_id == self.scope.scope_id, ImageProcessingJob.id == UUID(str(job_id))))
            if job is None:
                return None
            stages = self._stages(session, job.id)
            stage_names = {item.stage for item in stages if isinstance(item.stage, str)}
            auto_name = bool(getattr(job, "auto_name", False))
            # 旧三阶段历史只读合成跳过阶段，不写回数据库。
            if "auto_rename" not in stage_names:
                synthetic = ImageProcessingStage(scope_id=self.scope.scope_id, job_id=job.id, stage="auto_rename", status="skipped")
                stage_order = {name: index for index, name in enumerate(STAGES)}
                stages = sorted(
                    [*stages, synthetic],
                    key=lambda row: stage_order.get(row.stage, len(STAGES)) if isinstance(row.stage, str) else len(STAGES),
                )
            completed_stages = sum(isinstance(item.status, str) and item.status in STAGE_SETTLED for item in stages)
            progress = completed_stages / len(STAGES) if stages else None
            message = None
            if job.error and isinstance(job.error, Mapping):
                message = str(job.error.get("message") or job.error.get("error") or "图片处理失败")
            elif job.current_stage:
                message = f"阶段：{job.current_stage}"
            stage_payload_items: list[dict[str, object]] = []
            for item in stages:
                attempt = None
                if item.task_id:
                    attempt = session.scalar(
                        select(ImageProcessingAttempt)
                        .where(
                            ImageProcessingAttempt.scope_id == self.scope.scope_id,
                            ImageProcessingAttempt.task_id == item.task_id,
                            ImageProcessingAttempt.attempt == item.attempt_count,
                        )
                    )
                attempt_session_id = normalize_identifier(attempt.session_id, kind="session") if attempt else None
                attempt_executor_id = normalize_identifier(attempt.executor_attempt_id, kind="attempt") if attempt else None
                attempt_resume_available = bool(attempt and attempt.resume_available and attempt_session_id and attempt_executor_id)
                attempt_resume_reason = attempt.resume_reason if attempt else None
                if attempt and attempt.resume_available and not attempt_resume_available:
                    # 旧 attempt 的恢复标识损坏时，阶段详情也必须保持不可续跑。
                    attempt_resume_reason = "session_not_resumable"
                stage_payload_items.append(
                    {
                        "stage": item.stage,
                        "status": item.status,
                        "task_id": item.task_id,
                        "attempt": item.attempt_count,
                        "error": item.error,
                        "retry_at": item.retry_at,
                        # 这些字段来自服务端 attempt 事实，前端不能通过 stage
                        # payload 注入或替换恢复绑定。
                        "session_id": attempt_session_id,
                        "executor_attempt_id": attempt_executor_id,
                        "resume_available": attempt_resume_available,
                        "resume_reason": attempt_resume_reason,
                        "visual_match_snapshot": (
                            {
                                "protocol_version": attempt.visual_snapshot_protocol_version,
                                "snapshot_sha256": attempt.visual_snapshot_sha256,
                                "matched_at": attempt.visual_snapshot_matched_at,
                                "candidate_count": attempt.visual_snapshot_candidate_count,
                            }
                            if attempt is not None
                            and isinstance(attempt.visual_snapshot_sha256, str)
                            and len(attempt.visual_snapshot_sha256) == 64
                            and isinstance(attempt.visual_snapshot_protocol_version, int)
                            and isinstance(attempt.visual_snapshot_candidate_count, int)
                            and attempt.visual_snapshot_candidate_count >= 0
                            else None
                        ),
                    }
                )
            stage_payload = tuple(stage_payload_items)
            job_status = job.status if isinstance(job.status, str) else "failed"
            warning_visible = job_status not in {"failed", "blocked", "unknown_execution"}
            warnings = tuple(
                {
                    "stage": item.stage,
                    "error": (item.error if isinstance(item.error, Mapping) else {}).get("error", "auto_rename_warning"),
                    "message": "自动重命名未完成",
                    "recoverable": True,
                }
                for item in stages
                if warning_visible and item.stage == "auto_rename" and item.status == "warning"
            )
            try:
                public_policy = normalize_reverse_image_policy(job.reverse_image_policy)
            except ImageProcessingError:
                public_policy = "forbid"
            return ImageProcessingSnapshot(
                job_id=str(job.id),
                scope_id=self.scope.scope_id,
                meme_id=str(job.meme_id),
                revision=job.revision,
                image_sha256=job.image_sha256,
                reverse_image_policy=public_policy,
                status=job.status,
                current_stage=job.current_stage,
                stages=stage_payload,
                error=job.error,
                retry_at=job.retry_at,
                progress=progress,
                message=message,
                created_at=job.created_at,
                updated_at=job.updated_at,
                completed_at=job.completed_at,
                auto_name=auto_name,
                has_warnings=bool(warnings),
                warnings=warnings,
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
        if stage not in STAGES or status not in {"queued", "running", "succeeded", "failed", "blocked", "unknown_execution", "skipped", "warning"} or not isinstance(owner, str) or not owner or not isinstance(claim_generation, int) or claim_generation < 1:
            raise ImageProcessingError("invalid_stage_transition")
        if status == "skipped" and stage != "auto_rename":
            raise ImageProcessingError("invalid_stage_transition")
        if status == "warning":
            warning_code = error.get("error") if isinstance(error, Mapping) else None
            if stage != "auto_rename" or warning_code not in AUTO_RENAME_WARNING_ERRORS:
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
            pending_stage = next((item.stage for item in self._stages(session, job.id) if item.status not in STAGE_SETTLED), None)
            if pending_stage != stage:
                # 只允许固定顺序的第一个未收束阶段写回；旧叶子 Task
                # 不能越过前置阶段直接推进父 job。
                session.commit()
                return False
            allowed_transitions = {
                "queued": {"queued", "running", "succeeded", "failed", "blocked", "unknown_execution", "skipped", "warning"},
                "running": {"running", "succeeded", "failed", "blocked", "unknown_execution", "warning"},
                "succeeded": {"succeeded"},
                "failed": {"failed"},
                "blocked": {"blocked"},
                "unknown_execution": {"unknown_execution"},
                "skipped": {"skipped"},
                "warning": {"warning"},
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
            # warning 只属于可选阶段的历史事实；父 Job 仍可成功，顶层 error 必须为空。
            job.error = None if status in STAGE_SETTLED else error
            job.retry_at = retry_at
            job.updated_at = now
            if status in {"failed", "blocked", "unknown_execution"}:
                job.status = status
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
            elif status in STAGE_SETTLED and all(item.status in STAGE_SETTLED for item in self._stages(session, job.id)):
                job.status = "succeeded"
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
            elif status in STAGE_SETTLED:
                # 跳过阶段也属于已收束状态，current_stage 必须直接落到下一个
                # 尚未收束的阶段，避免 UI 在 Agent 成功后继续显示旧阶段。
                next_pending = next(
                    (item.stage for item in self._stages(session, job.id) if item.status not in STAGE_SETTLED),
                    None,
                )
                if next_pending is not None:
                    job.current_stage = next_pending
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

    def retry(self, job_id: UUID | str, *, policy: object = None, auto_name: object = None, config: Mapping[str, object] | None = None) -> ImageProcessingJob:
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
            old_auto_name = bool(getattr(old, "auto_name", False))
        return self.create_or_reuse(
            meme_id,
            sha,
            metadata_hash=metadata_hash,
            config=previous_config if config is None else config,
            reverse_image_policy=old_policy if policy is None else policy,
            auto_name=old_auto_name if auto_name is None else auto_name,
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

    def attach_task(
        self,
        job_id: UUID | str,
        stage: str,
        task_id: str,
        *,
        owner: str | None = None,
        claim_generation: int | None = None,
    ) -> bool:
        """把 pipeline 叶子 Task 绑定到同 scope 阶段并固化来源事实。

        只有没有来源、没有独立提交或其它 Job 关联的兼容历史 Task 才能被
        绑定。绑定同时更新 Task 专用来源列和 payload，避免后续查询依赖旧
        payload 猜测父 Job。Worker 提供 owner/generation 时，还必须持有父 Job
        的有效租约，防止过期 Worker 把新 claim 的叶子任务重新绑定。
        """
        if stage not in STAGES or not isinstance(task_id, str) or not task_id:
            raise ImageProcessingError("invalid_stage_transition")
        if (owner is None) != (claim_generation is None) or (owner is not None and (not owner or not isinstance(owner, str) or not isinstance(claim_generation, int) or claim_generation < 1)):
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
            if owner is not None:
                now = utcnow()
                if (
                    job.status != "running"
                    or job.lease_owner != owner
                    or job.claim_generation != claim_generation
                    or job.lease_expires_at is None
                    or job.lease_expires_at <= now
                ):
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
    """按 job scope 调度四阶段叶子 Task 的有界 Worker。"""

    def __init__(self, resources: Any, *, scope_id: ScopeContext | str, task_service: Any, policy: OperationPolicyGateway | None = None, grant_store: GrantAssociationStore | None = None, owner: str | None = None, max_workers: int = 2, handlers: Mapping[str, Callable[[dict[str, object]], object]] | None = None, task_handlers: Mapping[str, Callable[..., object]] | None = None, reconcile_interval: float = 2.0):
        """装配图片 Job repository、固定 stage plan 与叶子 task runner。"""
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.tasks = task_service
        self.jobs = ImageProcessingRepository(resources, self.scope)
        # stage plan 只描述顺序；具体 Job 的 auto_name 选项在 reconcile 时冻结。
        self.stage_plan = ImageStagePlan()
        self.policy = policy or OperationPolicyGateway(None)
        self.grants = grant_store or GrantAssociationStore()
        self.owner = owner or f"image-worker-{uuid4().hex}"
        self.handlers = dict(handlers or {})
        self.executor = ThreadPoolExecutor(
            max_workers=validate_agent_concurrency(max_workers),
            thread_name_prefix="mememeow-image-worker",
        )
        self._task_runner = None
        if callable(getattr(resources, "factory", None)):
            # 图片叶子任务使用独立 facade，但仍复用同一 PostgreSQL claim、lane
            # 和 fencing 原语；通用 manager 不会看到这些任务。
            from backend.pg_services import PostgresTaskService

            self._task_runner = PostgresTaskService(
                resources,
                scope_id=self.scope,
                agent_concurrency=int(getattr(task_service, "agent_concurrency", 1)),
                scope_concurrency=int(getattr(task_service, "agent_scope_concurrency", 1)),
                settings_version=getattr(task_service, "settings_version", None),
                lease_seconds=int(getattr(task_service, "lease_seconds", 120)),
                max_attempts=int(getattr(task_service, "max_attempts", 3)),
                executor=self.executor,
                finalize_image_tasks=False,
                operation_policy=self.policy,
                grant_store=self.grants,
                visual_snapshot_preparer=getattr(task_service, "_visual_snapshot_preparer", None),
                visual_candidate_preparer=getattr(task_service, "_visual_candidate_preparer", None),
                # 图片叶子 facade 必须继承应用级恢复开关和边界，否则完整
                # pipeline 会悄悄退回普通任务重试，绕过 session 续跑策略。
                resume_enabled=bool(getattr(task_service, "resume_enabled", False)),
                resume_max_attempts=int(getattr(task_service, "resume_max_attempts", 2)),
                resume_backoff_seconds=int(getattr(task_service, "resume_backoff_seconds", 2)),
                resume_max_backoff_seconds=int(getattr(task_service, "resume_max_backoff_seconds", 60)),
                resume_timeout_seconds=int(getattr(task_service, "resume_timeout_seconds", 900)),
            )
            for task_type, handler in (task_handlers or {}).items():
                self._task_runner.register(task_type, handler)
        self._stopped = threading.Event()
        self._reconcile_interval = max(0.25, min(float(reconcile_interval), 60.0))
        self._reconcile_thread: threading.Thread | None = None
        self._scheduled: set[str] = set()
        self._lock = threading.RLock()

    def submit(self, meme_id: UUID | str, image_sha256: str, *, metadata_hash: str | None = None, config: Mapping[str, object] | None = None, reverse_image_policy: object = None, auto_name: object = None, explicit_retry: bool = False, schedule: bool = True) -> ImageProcessingSnapshot:
        """创建/复用 job 并安排逐图处理，不等待任一叶子 Task。"""
        job = self.jobs.create_or_reuse(meme_id, image_sha256, metadata_hash=metadata_hash, config=config, reverse_image_policy=reverse_image_policy, auto_name=auto_name, explicit_retry=explicit_retry)
        if schedule:
            self.schedule(job.id)
        snapshot = self.jobs.snapshot(job.id)
        if snapshot is None:
            raise ImageProcessingError("job_not_found")
        return snapshot

    @staticmethod
    def _canonical_stage(stage: str) -> str:
        """把公开阶段名或任务类型收敛为固定 stage plan 的内部阶段。"""
        aliases = {
            "visual": "visual",
            "visual_embedding_generation": "visual",
            "agent": "agent",
            "meme_context_generation": "agent",
            "auto_rename": "auto_rename",
            "image_auto_rename": "auto_rename",
            "text_embedding": "text_embedding",
            "text_embedding_generation": "text_embedding",
        }
        if not isinstance(stage, str) or aliases.get(stage) is None:
            raise ImageProcessingError("invalid_image_stage")
        canonical = aliases[stage]
        normalize_stage(canonical)
        return canonical

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

    def _fail_unbound_pipeline_task(self, task_id: str, job: ImageProcessingJob, stage: str, code: str) -> bool:
        """仅失败归属于当前 Job 的 queued 叶子，清理绑定失败留下的孤儿任务。

        任务提交与 Job 阶段绑定跨越两个 repository 事务；若 grant 绑定或阶段
        绑定失败，必须先确认任务来源仍指向当前 Job，才允许收束它，避免误伤
        其它 Job 的同类型任务。任务若已被其它 Worker 认领则保留事实，由 claim
        fencing 和 Job 失败路径处理。
        """
        if not callable(getattr(self.resources, "factory", None)):
            return False
        with self.resources.factory() as session:
            task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == task_id,
                    Task.status == "queued",
                )
                .with_for_update()
            )
            if task is None:
                session.commit()
                return False
            payload = task.payload if isinstance(task.payload, Mapping) else {}
            if (
                task.task_type != STAGE_TASK_TYPES.get(stage)
                or task.submission_mode != "pipeline"
                or task.image_stage != stage
                or task.processing_job_id != job.id
                or payload.get("job_id") != str(job.id)
                or payload.get("meme_id") != str(job.meme_id)
                or payload.get("image_sha256") != job.image_sha256
            ):
                session.commit()
                return False
            now = utcnow()
            task.status = "failed"
            task.message = "图片阶段绑定失败"
            task.error = {"error": code}
            task.completed_at = now
            task.updated_at = now
            session.commit()
            return True

    def _attach_task_for_worker(self, job: ImageProcessingJob, stage: str, task_id: str) -> bool:
        """调用阶段绑定并在兼容测试 facade 上保留旧的三参数协议。"""
        attach = self.jobs.attach_task
        try:
            parameters = inspect.signature(attach).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "owner" in parameters or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            # 只有正常 Worker claim 才携带 generation；未认领的历史/测试 facade
            # 保留旧的准备协议，实际执行路径仍由 _run 先完成 job claim fencing。
            claim_generation = job.claim_generation
            if not isinstance(claim_generation, int) or claim_generation < 1:
                return attach(job.id, stage, task_id)
            return attach(job.id, stage, task_id, owner=self.owner, claim_generation=claim_generation)
        return attach(job.id, stage, task_id)

    def submit_stage(
        self,
        meme_id: UUID | str,
        stage: str,
        *,
        config: Mapping[str, object] | None = None,
        reverse_image_policy: object = None,
        auto_name: object = None,
        explicit_retry: bool = False,
        schedule: bool = True,
    ) -> Any:
        """提交或复用一个无父 Job 的独立图片阶段 Task。

        目标 SHA、阶段配置、处理选项和 scope 均由当前数据库 Meme 与服务端配置派生。
        活动任务按持久 dedupe key 复用；终态任务因不再属于活动集合而由同一
        请求创建新的逻辑 Task。只有 Agent 阶段会在 Task 建立后取得 grant。

        ``auto_name`` 会随共享处理确认写入独立 Task 输入，避免选项在公共 HTTP 边界
        丢失；独立阶段本身不负责编排自动命名的下游阶段。
        """
        del explicit_retry  # 终态任务天然脱离活动 dedupe 集合，显式参数仅保留 API 兼容性。
        canonical = self._canonical_stage(stage)
        task_type = STAGE_TASK_TYPES[canonical]
        policy = normalize_reverse_image_policy(reverse_image_policy)
        auto_name_value = normalize_auto_name(auto_name)
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
            metadata_hash = self._metadata_hash(meme) if canonical in {"text_embedding", "auto_rename"} else None

        payload: dict[str, object] = {
            "submission_mode": "standalone",
            "stage": canonical,
            "meme_id": str(identifier),
            "image_sha256": image_sha256,
            "processing_config_hash": config_hash,
            "reverse_image_policy": policy,
            "auto_name": auto_name_value,
            # 该 nonce 只用于识别并发 submit 的胜者，不进入 dedupe key。
            "standalone_submission_nonce": uuid4().hex,
        }
        if metadata_hash is not None:
            payload["metadata_hash"] = metadata_hash
        if canonical == "auto_rename":
            try:
                raw_context = meme.meme_context if isinstance(meme.meme_context, Mapping) else {}
                raw_title = raw_context.get("title") if isinstance(raw_context, Mapping) else None
                title = raw_title.strip() if isinstance(raw_title, str) else ""
            except (AttributeError, TypeError, ValueError):
                # 畸形语境的命名失败必须先落库叶子 Task，再由 handler 生成 warning。
                title = ""
            payload.update(
                {
                    "expected_storage_key": meme.storage_key,
                    "expected_meme_revision": meme.revision,
                    "title_fingerprint": stable_input_digest(title),
                }
            )
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
        if canonical == "agent":
            payload["visual_match_snapshot_protocol_version"] = 2
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
            request = self.policy.request(self.scope, Operations.ANALYSIS_AGENT, grant_key, resource_id=str(identifier), task_id=task_id, source="image-processing-standalone", input_digest=image_sha256)
            try:
                association = self.grants.get(request)
                if association is None:
                    association = self.grants.acquire(request, self.policy)
                if association.state in {"unknown", "released", "committed"}:
                    raise OperationPolicyError("operation_grant_invalid")
                if callable(getattr(self.grants, "bind_task", None)) and not self.grants.bind_task(association.grant, task_id):
                    raise OperationPolicyError("operation_grant_invalid")
            except OperationPolicyError as exc:
                self._release_uncommitted_grant(association)
                self._fail_unclaimed_task(task_id, exc.code)
                raise ImageProcessingError(exc.code, retry_at=exc.retry_at) from exc
            except Exception as exc:  # noqa: BLE001 - grant 绑定适配层必须 fail-closed
                self._release_uncommitted_grant(association)
                self._fail_unclaimed_task(task_id, "operation_policy_unavailable")
                raise ImageProcessingError("operation_policy_unavailable") from exc
            # 任务已创建后再把不透明 grant key 写入服务端 payload；key 不是 grant
            # 凭据，handler 仍会从服务端 grant store 取回真正授权。
            if callable(getattr(self.resources, "factory", None)):
                try:
                    with self.resources.factory() as session:
                        task = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
                        if task is not None:
                            next_payload = dict(task.payload or {})
                            next_payload["agent_grant_key"] = grant_key
                            task.payload = next_payload
                            session.commit()
                except Exception as exc:  # noqa: BLE001 - grant key 未持久化时禁止排队执行
                    self._release_uncommitted_grant(association)
                    self._fail_unclaimed_task(task_id, "operation_policy_unavailable")
                    raise ImageProcessingError("operation_policy_unavailable") from exc

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

    def _release_uncommitted_grant(self, association: GrantAssociation | None) -> None:
        """在叶子 Task 尚未进入外部执行前补偿释放 Agent grant。"""
        if association is None or association.state != "acquired":
            return
        try:
            result = self.policy.release(association.grant)
            if not result.ok or result.state not in {"released", "already_released"}:
                self._mark_grant_unknown(association)
                return
            if callable(getattr(self.grants, "transition", None)) and not self.grants.transition(association.grant, "released"):
                self._mark_grant_unknown(association)
        except Exception:  # noqa: BLE001 - 补偿结果不确定时必须禁止再次执行
            self._mark_grant_unknown(association)

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
            # 阶段产物绑定的是数据库中的 SHA；复用前仍要核对当前文件字节，
            # 否则文件被外部替换后可能把旧向量误判为当前版本有效。
            if not image_file_matches(self.resources, self.scope, meme):
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

            if stage == "auto_rename":
                # 自动重命名的目标名称必须在叶子 Task claim 内重新派生，不能
                # 仅凭旧 storage key 把用户手动命名误判为成功。
                return False

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
            snapshot = self.jobs.snapshot(job.id)
            if snapshot is None:
                return
            statuses = {str(item["stage"]): str(item["status"]) for item in snapshot.stages if isinstance(item, Mapping)}
            plan = ImageStagePlan(auto_name=bool(getattr(job, "auto_name", False)))
            # 失败/阻止/未知阶段必须等待显式重试；不能因 reconcile 扫描再次创建
            # 同一图片版本的 provider Task，避免重复外部副作用和重复计量。
            if plan.blocked(statuses):
                return
            name = plan.next_stage(statuses)
            if name is None:
                return
            stage = next((item for item in snapshot.stages if item.get("stage") == name), None)
            if stage is None:
                return
            task_id = stage.get("task_id")
            if name == "text_embedding":
                # Agent 写回后即使自动命名被跳过，也必须以当前实际 Meme
                # storage key 重新计算 metadata hash 再冻结文本 Task 输入。
                self._refresh_metadata_hash(job)

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
                if name == "auto_rename":
                    self._refresh_metadata_hash(job)
                if name != STAGES[-1]:
                    reschedule = True
                return
            if child is not None and child.status == "failed":
                child_error = child.error if isinstance(child.error, dict) else {"error": "stage_failed"}
                stage_error = str(child_error.get("error") or "stage_failed")
                terminal_status = "unknown_execution" if stage_error in {"unknown_execution", "reverse_image_unknown_execution", "auto_rename_unknown_execution"} else "failed"
                if name == "auto_rename" and stage_error in {"auto_rename_title_missing", "auto_rename_invalid_filename", "auto_rename_target_exists", "auto_rename_target_changed"}:
                    terminal_status = "warning"
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, task_id=str(task_id), error={"error": stage_error})
                if name == "auto_rename" and terminal_status == "warning":
                    self._refresh_metadata_hash(job)
                    reschedule = True
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
                handler_payload = dict(getattr(child, "payload", {}) or {}) if child is not None else {}
                handler_payload.update({"job_id": str(job.id), "task_id": str(task_id), "stage": name, "scope_id": self.scope.scope_id, "image_sha256": job.image_sha256, "reverse_image_policy": normalize_reverse_image_policy(job.reverse_image_policy)})
                handler(handler_payload)
            except ImageProcessingError as exc:
                terminal_status = "warning" if name == "auto_rename" and exc.code in {"auto_rename_title_missing", "auto_rename_invalid_filename", "auto_rename_target_exists", "auto_rename_target_changed"} else exc.code if exc.code in {"blocked", "unknown_execution"} else "failed"
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, task_id=str(task_id), error={"error": exc.code}, retry_at=exc.retry_at)
                if terminal_status == "warning":
                    self._refresh_metadata_hash(job)
                    reschedule = True
            except Exception as exc:  # noqa: BLE001 - 叶子错误不泄露原文
                raw_code = getattr(exc, "code", None) or str(exc).split(":", 1)[0]
                code = raw_code if isinstance(raw_code, str) else ""
                if name == "auto_rename" and code in AUTO_RENAME_WARNING_ERRORS:
                    terminal_status = "warning"
                elif name == "auto_rename" and code in AUTO_RENAME_UNKNOWN_ERRORS:
                    terminal_status = "unknown_execution"
                else:
                    terminal_status = "failed"
                    code = "stage_failed"
                self.jobs.transition(job.id, name, owner=self.owner, claim_generation=job.claim_generation, status=terminal_status, task_id=str(task_id), error={"error": code})
                if terminal_status == "warning":
                    self._refresh_metadata_hash(job)
                    reschedule = True
                logger.info("image_processing_stage_failed job=%s stage=%s error=%s", job.id, name, type(exc).__name__)
            else:
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
            if self.jobs.update_metadata_hash(job.id, owner=self.owner, claim_generation=job.claim_generation, metadata_hash=current):
                job.metadata_hash = current

    @staticmethod
    def _task_dedupe_key(task_type: str, payload: Mapping[str, object]) -> str:
        """按 PostgreSQL 任务 facade 的规则生成活动叶子 Task 去重键。

        提交来源参与 key，避免同一图片阶段的 Job 子任务和独立任务互相复用。
        """
        mode = payload.get("submission_mode") or ("pipeline" if payload.get("job_id") else "legacy")
        stage = payload.get("stage") or {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
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
        if stage == "auto_rename":
            with self.resources.factory() as session:
                meme = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == job.meme_id))
            if meme is None or meme.sha256.lower() != job.image_sha256.lower():
                raise ImageProcessingError("target_changed")
            try:
                raw_context = meme.meme_context if isinstance(meme.meme_context, Mapping) else {}
                raw_title = raw_context.get("title") if isinstance(raw_context, Mapping) else None
                title = raw_title.strip() if isinstance(raw_title, str) else ""
            except (AttributeError, TypeError, ValueError):
                # 独立重试也必须持久化失败叶子，不能在构造 payload 时丢失阶段事实。
                title = ""
            payload.update(
                {
                    "expected_storage_key": meme.storage_key,
                    "expected_meme_revision": meme.revision,
                    "title_fingerprint": stable_input_digest(title),
                }
            )
        if stage == "agent":
            payload["visual_match_snapshot_protocol_version"] = 2
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
                "auto_name",
                "processing_config_hash",
                "session_id",
                "attempt",
            }:
                payload.setdefault(str(key), value)
        submitter = self._task_runner or self.tasks
        dedupe_key = self._task_dedupe_key(task_type, payload)
        active_task_id = self._find_active_task(submitter, task_type, dedupe_key)
        if active_task_id is not None:
            if not self._attach_task_for_worker(job, stage, active_task_id):
                raise ImageProcessingError("stage_task_bind_failed")
            return active_task_id

        association: GrantAssociation | None = None
        gateway_request = None
        if stage == "agent":
            logical_key = f"agent:{job.meme_id}:{job.image_sha256}:{job.processing_config_hash}:{policy}:r{job.revision}"
            # 任务尚未提交时不能把 job UUID 写入 operation_grants.task_id 外键；
            # 先取得按 logical key 去重的 grant，提交后再绑定真实 Task ID。
            gateway_request = self.policy.request(self.scope, Operations.ANALYSIS_AGENT, logical_key, resource_id=str(job.meme_id), source="image-processing", input_digest=job.image_sha256)
            try:
                association = self.grants.get(gateway_request)
                if association is None:
                    if hasattr(self.grants, "acquire"):
                        association = self.grants.acquire(gateway_request, self.policy)
                    else:
                        grant = require_allowed(self.policy.acquire(gateway_request))
                        association = self.grants.put(GrantAssociation(gateway_request, grant))
            except OperationPolicyError as exc:
                if exc.code == "operation_policy_unavailable":
                    # 另一个进程可能已经把同一逻辑 grant 绑定到叶子 Task，但
                    # 当前事务尚未在活动 Task 索引中观察到；重新读取精确 dedupe
                    # 事实后才能收敛并发提交，不能把可证明的重复误报为策略损坏。
                    rebound_task_id = self._find_active_task(submitter, task_type, dedupe_key)
                    if rebound_task_id is not None and self._attach_task_for_worker(job, stage, rebound_task_id):
                        return rebound_task_id
                self.jobs.transition(job.id, stage, owner=self.owner, claim_generation=job.claim_generation, status="blocked", error={"error": exc.code}, retry_at=exc.retry_at)
                raise ImageProcessingError("blocked", retry_at=exc.retry_at) from exc
            if association is not None and association.state in {"unknown", "committed"}:
                raise ImageProcessingError("unknown_execution" if association.state == "unknown" else "operation_grant_invalid")
            if association is not None and association.state == "released":
                raise ImageProcessingError("operation_grant_invalid")
            # grant 只保存在服务端 association，绝不进入普通 Task payload。
        try:
            record = submitter.submit(task_type, payload, schedule=False)
        except Exception:
            if association is not None and association.state == "acquired":
                self._release_uncommitted_grant(association)
            raise
        task_id = self._task_identifier(record)
        if task_id is None:
            self._mark_grant_unknown(association)
            raise ImageProcessingError("stage_task_create_failed")
        if association is not None and callable(getattr(self.grants, "bind_task", None)):
            try:
                bound = self.grants.bind_task(association.grant, task_id)
            except Exception as exc:  # noqa: BLE001 - grant 绑定结果未知时必须 fail-closed
                logger.warning("image_processing_grant_bind_unknown task=%s error=%s", task_id, type(exc).__name__)
                bound = False
            if not bound:
                # Task 已经持久化但尚未 claim，仍未达到 Agent 副作用边界；先补偿
                # 释放授权，释放不确定时再收束 unknown。
                self._release_uncommitted_grant(association)
                self._fail_unbound_pipeline_task(task_id, job, stage, "stage_grant_bind_failed")
                raise ImageProcessingError("stage_grant_bind_failed")
        if not self._attach_task_for_worker(job, stage, task_id):
            self._mark_grant_unknown(association)
            self._fail_unbound_pipeline_task(task_id, job, stage, "stage_task_bind_failed")
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
        """停止新 job 认领并等待图片叶子任务退出，再释放线程池。"""
        self._stopped.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=2)
        if self._task_runner is not None:
            self._task_runner.shutdown()
        # 线程仍可能持有 PostgreSQL session；数据库连接池必须在它们退出后再销毁。
        self.executor.shutdown(wait=True, cancel_futures=True)


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
