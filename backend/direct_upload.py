# 直接上传的向量提交入口：两种向量共享上传事实，各自独立执行。

from dataclasses import dataclass

from sqlalchemy import select

from backend.persistence.engine import DatabaseError
from backend.persistence.models import Meme, MemeVisualEmbedding
from backend.persistence.resources import DatabaseResources
from backend.text_updates import enqueue_text_update


@dataclass(frozen=True)
class DirectUploadResult:
    """直接上传返回的独立任务及当前文本资格，无父处理 Job。"""

    task_ids: tuple[str, ...]
    text_input_status: str
    job_id: None = None
    status: str = "queued"


def submit_direct_upload(resources: DatabaseResources, snapshot: Meme, *, identity: object) -> DirectUploadResult:
    """在同一短事务中检查图片身份并分别复用或提交两类向量。"""
    with resources.environment(snapshot.scope_id) as environment:
        environment.tasks._lock_image_submission(snapshot.id, snapshot.sha256)
        meme = environment.memes.get(snapshot.id, for_update=True)
        if meme is None or meme.sha256 != snapshot.sha256:
            raise DatabaseError("target_changed")
        task_ids = []
        visual = environment.uow.session.scalar(select(MemeVisualEmbedding).where(
            MemeVisualEmbedding.scope_id == meme.scope_id,
            MemeVisualEmbedding.meme_id == meme.id,
            MemeVisualEmbedding.image_sha256 == meme.sha256,
            MemeVisualEmbedding.model == identity.model,
            MemeVisualEmbedding.preprocess_version == identity.preprocess_version,
            MemeVisualEmbedding.dimensions == identity.dimensions,
        ))
        if visual is None:
            task = environment.tasks.submit(task_type="visual_embedding_generation", max_attempts=1,
                submission_mode="standalone", image_stage="visual", target_meme_id=meme.id, target_image_sha256=meme.sha256,
                dedupe_key=f"direct-visual:{meme.id}:{meme.sha256}:{identity.model}:{identity.preprocess_version}",
                payload={"submission_mode": "standalone", "stage": "visual", "meme_id": str(meme.id), "image_sha256": meme.sha256, "visual_model": identity.model, "visual_dimensions": identity.dimensions, "preprocess_version": identity.preprocess_version})
            task_ids.append(task.id)
        text_task = enqueue_text_update(environment.uow.session, meme)
        if text_task is not None:
            task_ids.append(text_task.id)
        return DirectUploadResult(tuple(task_ids), meme.text_input_status, status="queued" if task_ids else "succeeded")
