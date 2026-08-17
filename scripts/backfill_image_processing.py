"""分页 seed 图片处理 job 并回填当前 scope 的单图文本向量。

该脚本位于部署迁移工具目录，读取 PostgreSQL 当前 Meme 记录作为唯一事实来源。
它使用 keyset 分页和持久化迁移 epoch，单页之外不保存全库图片 ID；外部 embedding
调用通过服务端配置创建，不接受命令行传入的 provider 密钥或普通 payload 授权字段。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select

if __package__ in {None, ""}:
    # 允许维护者直接执行脚本，同时保持 ``python -m`` 的标准入口。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Settings
from backend.database import DatabaseError, DatabaseResources, ImageProcessingStage, Meme, StorageOperation, Task, create_engine_for_settings
from backend.image_processing import ImageProcessingError, ImageProcessingRepository, SingleImageEmbeddingService
from backend.metadata import MetadataError
from backend.pg_services import PostgresMetadataService, PostgresSearchService
from backend.visual import identity_from_settings


LOGGER = logging.getLogger("mememeow.image_backfill")
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
IMAGE_TASK_STAGES = {
    "visual_embedding_generation": "visual",
    "meme_context_generation": "agent",
    "text_embedding_generation": "text_embedding",
}


def backfill_task_sources(resources: DatabaseResources, scope_id: str) -> dict[str, int]:
    """按可信阶段关系回填 pipeline 来源，并标记其余历史阶段为未归类。

    只有 ``image_processing_stages.task_id`` 的复合 scope 关系能证明父 Job
    归属；孤立任务不补写 standalone 或 pipeline，保留 NULL 来源供只读诊断。
    """
    linked = 0
    unclassified = 0
    with resources.factory() as session:
        linked_rows = list(
            session.execute(
                select(ImageProcessingStage, Task)
                .join(
                    Task,
                    (Task.scope_id == ImageProcessingStage.scope_id)
                    & (Task.id == ImageProcessingStage.task_id),
                )
                .where(
                    ImageProcessingStage.scope_id == scope_id,
                    ImageProcessingStage.task_id.is_not(None),
                )
            )
        )
        linked_task_ids: set[str] = set()
        for stage, task in linked_rows:
            expected = IMAGE_TASK_STAGES.get(task.task_type)
            if expected != stage.stage:
                continue
            linked_task_ids.add(task.id)
            if task.submission_mode is None and task.processing_job_id is None:
                task.submission_mode = "pipeline"
                task.image_stage = stage.stage
                task.processing_job_id = stage.job_id
                payload = dict(task.payload or {})
                payload.update({"submission_mode": "pipeline", "stage": stage.stage, "job_id": str(stage.job_id)})
                task.payload = payload
                linked += 1

        historical = list(
            session.scalars(
                select(Task).where(
                    Task.scope_id == scope_id,
                    Task.task_type.in_(tuple(IMAGE_TASK_STAGES)),
                    Task.submission_mode.is_(None),
                )
            )
        )
        for task in historical:
            if task.id in linked_task_ids:
                continue
            stage = IMAGE_TASK_STAGES[task.task_type]
            if task.image_stage is None:
                task.image_stage = stage
                payload = dict(task.payload or {})
                payload.setdefault("stage", stage)
                task.payload = payload
            unclassified += 1
        session.commit()
    return {"pipeline_linked": linked, "unclassified": unclassified}


def _visible_filter(scope_id: str):
    """返回排除正在跨存储提交的 Meme 的 scope 条件。"""
    active_operation = select(StorageOperation.id).where(
        StorageOperation.scope_id == scope_id,
        StorageOperation.meme_id == Meme.id,
        StorageOperation.status.in_(("prepared", "file_applied")),
    ).exists()
    return (Meme.scope_id == scope_id, ~active_operation)


def count_current_memes(resources: DatabaseResources, scope_id: str) -> int:
    """统计当前 scope 可迁移图片数量，不读取全库主键列表。"""
    with resources.factory() as session:
        return int(session.scalar(select(func.count()).select_from(Meme).where(*_visible_filter(scope_id))) or 0)


def iter_meme_pages(resources: DatabaseResources, scope_id: str, *, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[list[Meme]]:
    """按 storage key 和 meme UUID 稳定 keyset 分页返回当前 scope 图片。"""
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    last_key: str | None = None
    last_id: UUID | None = None
    while True:
        with resources.factory() as session:
            conditions = list(_visible_filter(scope_id))
            if last_key is not None and last_id is not None:
                conditions.append(or_(Meme.storage_key > last_key, (Meme.storage_key == last_key) & (Meme.id > last_id)))
            rows = list(
                session.scalars(
                    select(Meme).where(*conditions).order_by(Meme.storage_key.asc(), Meme.id.asc()).limit(page_size)
                )
            )
        if not rows:
            return
        yield rows
        last_key = rows[-1].storage_key
        last_id = rows[-1].id


def _processing_config(settings: Settings) -> dict[str, object]:
    """构造迁移 job 使用的服务端配置指纹输入。"""
    identity = identity_from_settings(settings)
    return {
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": 1024,
        "visual_model": identity.model,
        "visual_dimensions": identity.dimensions,
        "preprocess_version": identity.preprocess_version,
        "settings_version": settings.settings_version,
        "migration_source": "image-backfill",
    }


def run_backfill(
    resources: DatabaseResources,
    settings: Settings,
    *,
    scope_id: str = "local",
    page_size: int = DEFAULT_PAGE_SIZE,
    seed_only: bool = False,
    switch: bool = False,
) -> dict[str, object]:
    """执行一轮分页 seed/向量回填，并按需原子切换迁移来源。"""
    total = count_current_memes(resources, scope_id)
    config = _processing_config(settings)
    jobs = ImageProcessingRepository(resources, scope_id)
    metadata = PostgresMetadataService(resources, scope_id=scope_id)
    search = PostgresSearchService(settings, resources, metadata, scope_id=scope_id)
    embedder = None if seed_only else search._embedding
    embedding_service = None if embedder is None else SingleImageEmbeddingService(resources, scope_id=scope_id, model=settings.embedding_model, dimensions=1024, embedder=embedder)

    with resources.environment(scope_id) as environment:
        state = environment.search.begin_incremental_backfill(settings.embedding_model, total_count=total)
        epoch = state.epoch

    processed = 0
    seeded = 0
    embedded = 0
    failures: list[dict[str, str]] = []
    for page in iter_meme_pages(resources, scope_id, page_size=page_size):
        for meme in page:
            processed += 1
            try:
                image = metadata.blob_store.resolve(meme.storage_key)
                record = metadata.embedding_record(image)
                metadata_hash = record.get("metadata_hash") if isinstance(record.get("metadata_hash"), str) else None
                jobs.create_or_reuse(
                    meme.id,
                    meme.sha256,
                    metadata_hash=metadata_hash,
                    config=config,
                    reverse_image_policy="forbid",
                    explicit_retry=False,
                )
                seeded += 1
                if embedding_service is not None:
                    if not record.get("indexable") or not metadata_hash:
                        raise ImageProcessingError("query_embedding_not_ready")
                    embedding_service.upsert(
                        meme.id,
                        image_sha256=meme.sha256,
                        metadata_hash=metadata_hash,
                        semantic_document=str(record.get("text") or ""),
                    )
                    embedded += 1
            except (DatabaseError, MetadataError, ImageProcessingError, OSError, ValueError, TypeError) as exc:
                failures.append({"meme_id": str(meme.id), "error": getattr(exc, "code", type(exc).__name__)})
        with resources.environment(scope_id) as environment:
            if not environment.search.record_incremental_backfill(epoch=epoch, completed_count=processed, total_count=total, model=settings.embedding_model):
                raise DatabaseError("migration_epoch_stale")
        LOGGER.info("image_backfill_progress processed=%d total=%d seeded=%d embedded=%d failed=%d", processed, total, seeded, embedded, len(failures))

    switched = False
    if switch and not seed_only and processed == total and not failures:
        with resources.environment(scope_id) as environment:
            switched = environment.search.switch_incremental_only(epoch=epoch, model=settings.embedding_model)
    source_backfill = backfill_task_sources(resources, scope_id)
    return {
        "scope_id": scope_id,
        "model": settings.embedding_model,
        "epoch": epoch,
        "total": total,
        "processed": processed,
        "seeded": seeded,
        "embedded": embedded,
        "failed": len(failures),
        "failures": failures[:100],
        "switched": switched,
        "mode": "incremental_only" if switched else "backfill",
        **source_backfill,
    }


def _parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="分页 seed 图片处理 job 并回填单图文本向量")
    parser.add_argument("--scope-id", default="local")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--seed-only", action="store_true", help="只创建图片 job，不调用 embedding provider")
    parser.add_argument("--switch", action="store_true", help="全部图片成功回填后切换到 incremental_only")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行迁移命令并返回进程退出码。"""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.page_size < 1 or args.page_size > MAX_PAGE_SIZE:
        parser.error(f"--page-size 必须在 1 到 {MAX_PAGE_SIZE} 之间")
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s %(message)s")
    settings = Settings(_env_file=None)
    engine = create_engine_for_settings(settings)
    resources = None
    try:
        resources = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings, require_local_scope=False)
        result = run_backfill(resources, settings, scope_id=args.scope_id, page_size=args.page_size, seed_only=args.seed_only, switch=args.switch)
        LOGGER.info("image_backfill_finished processed=%s embedded=%s failed=%s switched=%s", result["processed"], result["embedded"], result["failed"], result["switched"])
        return 0 if not result["failed"] else 2
    except (DatabaseError, OSError, ValueError) as exc:
        LOGGER.error("image_backfill_failed error=%s", getattr(exc, "code", type(exc).__name__))
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
