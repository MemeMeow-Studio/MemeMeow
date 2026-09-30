# 上传请求只负责接收受限文件与持久任务登记，图片解码在后台执行。

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from backend.image_upload_http import MAX_UPLOAD_FILES_PER_REQUEST, _parse_upload_form
from backend.image_processing import ImageProcessingError
from backend.operation_policy import OperationPolicyError
from backend.persistence.engine import DatabaseError
from backend.upload_receipts import UploadReceipts
from backend.gif_compression import gif_upload_limit


async def receive_uploads(request: Request, *, services: Any, options: Callable[..., Any], parse_bool: Callable[..., bool], sanitize: Callable[[str], str], authorization: Any, error: Callable[..., Any], allow_model: bool = False) -> dict[str, Any]:
    """解析受限 multipart 并逐文件确认持久接收，返回批次和后台任务标识。"""
    settings = request.app.state.settings
    limit = min(MAX_UPLOAD_FILES_PER_REQUEST, max(1, settings.max_files_per_request))
    form = await _parse_upload_form(request, max_files=limit, max_request_bytes=settings.max_request_bytes or settings.max_upload_size * limit, error=error)
    try:
        if set(form) - {"files", "auto_name", "reverse_image_policy", "model", "request_id", "upload_mode"}:
            raise error(400, "invalid_request", "上传请求包含未知字段")
        try:
            request_id = UUID(str(form.get("request_id") or uuid4())).hex
        except ValueError as exc:
            raise error(400, "invalid_request_id", "上传请求标识必须为 UUID") from exc
        options_kwargs = {"reverse_image_policy": form.get("reverse_image_policy"), "auto_name": parse_bool(form.get("auto_name"), default=False), "upload_mode": form.get("upload_mode", "analyze")}
        if form.get("model") is not None:
            if not allow_model:
                raise error(400, "invalid_request", "上传请求不支持 model 字段")
            options_kwargs["model"] = form.get("model")
        try:
            normalized = await run_in_threadpool(options, request, **options_kwargs)
        except ImageProcessingError as exc:
            raise error(400, exc.code, str(exc)) from exc
        frozen = {"reverse_image_policy": normalized.reverse_image_policy, "auto_name": normalized.auto_name, "upload_mode": normalized.upload_mode}
        if getattr(normalized, "model", None) is not None:
            frozen["model"] = normalized.model
        files = [item for item in form.getlist("files") if hasattr(item, "file")]
        if not files:
            raise error(400, "files_required", "必须上传图片文件")
        receipts = await run_in_threadpool(UploadReceipts, services.tasks.resources, services.scope, services.tasks)
        results = []
        for index, upload in enumerate(files):
            original = upload.filename or "image"
            filename = sanitize(original)
            if not filename or Path(filename).name != filename:
                results.append({"filename": original, "ok": False, "error": "invalid_filename"})
                continue
            try:
                max_bytes = gif_upload_limit(upload.file, settings.max_upload_size)
                result = await run_in_threadpool(receipts.accept, upload.file, request_id=request_id, index=index, filename=filename, options=frozen, size_bytes=upload.size, max_bytes=max_bytes, max_pending_bytes=settings.max_pending_upload_bytes, reserve=authorization.reserve)
            except (DatabaseError, OperationPolicyError) as exc:
                result = {"filename": original, "ok": False, "error": exc.code}
            results.append(result)
        return {"batch_id": request_id, "results": results}
    finally:
        await form.close()
