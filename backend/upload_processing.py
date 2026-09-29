# 上传后台业务：复用持久 Task，依次登记图片并提交既有图片处理 Job。

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from loguru import logger

from backend.image_safety import validate_image_content
from backend.collection_packages import sha256_bytes
from backend.persistence.engine import DatabaseError
from backend.persistence.file_lock import shared_file_lock
from backend.upload_receipts import UploadReceipts


def prepare_upload(content: bytes, filename: str) -> tuple[bytes, str, str | None]:
    """校验上传图片，返回规范字节、存储名称和可选展示名称。"""
    validate_image_content(content, Path(filename).suffix.lower())
    return content, filename, None


def run_upload_processing(
    services: Any,
    payload: dict[str, Any],
    progress: Callable[..., Any],
    *,
    prepare: Callable[..., tuple[bytes, str, str | None]],
    reserve: Callable[[dict[str, Any]], None],
    commit: Callable[[dict[str, Any], str, bool], None],
    submit_processing: Callable[..., Any],
) -> dict[str, Any]:
    """由可信 Task 服务处理已保存输入，文件锁串行化同一接收记录的重试。"""
    receipts = UploadReceipts(services.tasks.resources, services.scope, services.tasks)
    receipt_id = payload["receipt_id"]
    with shared_file_lock(receipts.root / f"{receipt_id}.lock"):
        def ensure_active(*, submitting: bool = False) -> None:
            """在阶段边界检查租约与取消请求，最终提交与取消使用同一行锁。"""
            with receipts.resources.environment(services.scope) as environment:
                task = environment.tasks.get(payload["_claim_task_id"], for_update=True)
                if not environment.tasks.update_payload_fenced(payload["_claim_task_id"], payload["_claim_generation"], payload["_claim_owner"], {}):
                    raise DatabaseError("claim_expired")
                if task.payload.get("cancel_requested"):
                    raise DatabaseError("task_cancelled")
                if submitting:
                    receipt = environment.tasks.get(receipt_id, for_update=True)
                    receipt.payload = {**receipt.payload, "phase": "submitting"}

        ensure_active()
        with receipts.resources.environment(services.scope) as environment:
            receipt = environment.tasks.get(receipt_id)
            if receipt is None:
                raise DatabaseError("upload_receipt_missing")
            durable = dict(receipt.payload)
        durable["_claim_task_id"] = payload["_claim_task_id"]
        if durable.get("phase") == "receiving" and not durable.get("input_digest"):
            if not receipts.path(receipt_id).exists():
                raise DatabaseError("upload_receiving_interrupted")
            durable["input_digest"] = sha256_bytes(receipts.read(durable))
            with receipts.resources.environment(services.scope) as environment:
                receipt = environment.tasks.get(receipt_id, for_update=True)
                receipt.payload = {**receipt.payload, "input_digest": durable["input_digest"], "phase": "accepted"}
        reserve(durable)
        progress(0.1, "校验图片内容")
        content, target_key, display_name = prepare(receipts.read(durable), durable["filename"])
        digest = sha256_bytes(content)
        ensure_active()
        existing = services.metadata.find_existing_upload(target_key, sha256=digest, size_bytes=len(content))
        if existing is not None:
            record, image = existing
            if display_name is not None and record.display_name != display_name:
                raise DatabaseError("image_exists")
            owned = (record.provenance or {}).get("upload_receipt_id") == receipt_id
        else:
            progress(0.3, "保存图片记录")
            with receipts.resources.environment(services.scope) as environment:
                receipt = environment.tasks.get(receipt_id, for_update=True)
                receipt.payload = {**receipt.payload, "phase": "writing", "target_key": target_key, "normalized_digest": digest, "normalized_size": len(content)}
            kwargs = {"display_name": display_name} if display_name is not None else {}
            meme_id, image = services.metadata.upload_bytes(content, target_key=target_key, receipt_id=receipt_id, **kwargs)
            record, image = services.metadata.image_for_meme(meme_id)
            owned = True
        meme_id = str(record.id)
        with receipts.resources.environment(services.scope) as environment:
            receipt = environment.tasks.get(receipt_id, for_update=True)
            receipt.payload = {**receipt.payload, "phase": "registered", "meme_id": meme_id, "owns_image": owned}
        commit(durable, meme_id, owned)
        ensure_active(submitting=True)
        progress(0.7, "提交图片处理任务")
        services.thumbnails.enqueue(meme_id)
        processing = submit_processing(record, image, durable["options"])
        result = {"filename": durable["filename"], "batch_id": durable["batch_id"], "meme_id": meme_id, "image_sha256": digest, "processing_job_id": processing.job_id, "processing_status": processing.status, "saved_filename": services.metadata.saved_filename(record)}
        result["upload_mode"] = durable["options"].get("upload_mode", "analyze")
        if result["upload_mode"] == "direct":
            result.update(task_ids=processing.task_ids, text_input_status=processing.text_input_status)
        with receipts.resources.environment(services.scope) as environment:
            receipt = environment.tasks.get(receipt_id, for_update=True)
            receipt.payload = {**receipt.payload, "phase": "submitted", "processing_job_id": processing.job_id, "processing_result": result}
        logger.info("upload_processing_submitted task_id={} meme_id={} processing_job_id={}", payload["_claim_task_id"], meme_id, processing.job_id)
        return result


def reconcile_upload(services: Any, task_id: str, authorization: Any) -> None:
    """对终态上传结算容量并清理输入；失败输入保留七天供显式重试。"""
    from datetime import timedelta
    from sqlalchemy import select
    from backend.persistence.models import Task, utcnow

    receipts = UploadReceipts(services.tasks.resources, services.scope, services.tasks)
    with receipts.resources.environment(services.scope) as environment:
        task = environment.tasks.get(task_id)
        if task is None:
            return
        receipt_id = task.payload["receipt_id"]
    with shared_file_lock(receipts.root / f"{receipt_id}.lock", blocking=False) as acquired:
        if not acquired:
            return
        with receipts.resources.environment(services.scope) as environment:
            active = environment.uow.session.scalar(select(Task.id).where(Task.scope_id == services.scope.scope_id, Task.task_type == "image_upload", Task.payload["receipt_id"].astext == receipt_id, Task.status.in_(("queued", "running"))).limit(1))
            task = environment.tasks.get(task_id)
            receipt = environment.tasks.get(receipt_id)
            if active is not None or task is None or receipt is None or receipt.payload.get("input_removed"):
                return
            durable = dict(receipt.payload)
            succeeded = task.status == "succeeded"
            expired = receipt.created_at < utcnow() - timedelta(days=7)
        phase = durable.get("phase")
        if phase == "writing":
            services.metadata.recover_storage(limit=500)
            existing = services.metadata.find_existing_upload(durable["target_key"], sha256=durable["normalized_digest"], size_bytes=durable["normalized_size"])
            if existing is None:
                authorization.discard(durable)
            else:
                record, _image = existing
                authorization.finish(durable, str(record.id), (record.provenance or {}).get("upload_receipt_id") == receipt_id)
        elif phase in {"registered", "submitting", "submitted"}:
            authorization.finish(durable, durable["meme_id"], bool(durable["owns_image"]))
        else:
            authorization.discard(durable)
        incomplete = phase == "receiving" and not receipts.path(receipt_id).exists()
        if succeeded or expired or incomplete:
            receipts.path(receipt_id).unlink(missing_ok=True)
            receipts.path(receipt_id).with_suffix(".receiving").unlink(missing_ok=True)
            with receipts.resources.environment(services.scope) as environment:
                receipt = environment.tasks.get(receipt_id, for_update=True)
                receipt.payload = {**receipt.payload, "input_removed": True}
