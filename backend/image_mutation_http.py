"""公共核心图片重命名与删除 HTTP 边界。

本模块负责按稳定 ``meme_id`` 执行图片变更的请求校验、文件目标检查、错误投影和副作用
顺序；scope-bound metadata service、文件名规范化、operation policy、检索失效与路由注册
由入口通过 callback 注入。本模块不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError, Operations


MetadataServiceProvider = Callable[[Request], Any]
FilenameSanitizer = Callable[[str], str]
StorageKeyValidator = Callable[[str], str]
SearchInvalidator = Callable[[Request], None]
ErrorFactory = Callable[[int, str, str], HTTPException]
OperationAcquire = Callable[..., Any]
OperationCommit = Callable[[Request, Any], None]
OperationRelease = Callable[[Request, Any], None]
OperationErrorProjector = Callable[[OperationPolicyError], HTTPException]


async def rename_image(
    request: Request,
    payload: Any,
    *,
    metadata_service: MetadataServiceProvider,
    sanitize_filename: FilenameSanitizer,
    validate_storage_key: StorageKeyValidator,
    invalidate_search: SearchInvalidator,
    error: ErrorFactory,
) -> dict[str, str]:
    """按当前 scope 的稳定 Meme ID 重命名图片并同步 metadata。

    输入是入口已校验的 ``meme_id`` 和新文件名；输出保留旧接口的稳定 ID、文件名及媒体
    URL。调用场景是图片工作区的人工重命名请求，目标记录和源文件由注入的当前 scope
    metadata service 派生，成功后的检索失效不会早于 metadata 持久化。
    """
    if not payload.meme_id:
        raise error(400, "meme_id_required", "必须提供 meme_id")
    scoped_metadata = metadata_service(request)
    try:
        _record, source = scoped_metadata.image_for_meme(payload.meme_id)
    except MetadataError as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc

    clean = sanitize_filename(payload.new_name)
    if Path(clean).suffix.lower() != source.suffix.lower():
        clean = f"{Path(clean).stem}{source.suffix.lower()}"
    if any(char in payload.new_name for char in "/\\") or any(ord(char) < 32 for char in payload.new_name):
        raise error(400, "invalid_filename", "文件名非法")
    try:
        validate_storage_key(clean)
    except ValueError as exc:
        raise error(400, "invalid_filename", "文件名非法") from exc

    target = scoped_metadata.blob_store.resolve(clean, must_exist=False)
    if target.exists() and target != source:
        raise error(409, "file_exists", "目标文件已存在")
    try:
        metadata = scoped_metadata.rename_by_id(payload.meme_id, target)
    except MetadataError as exc:
        if exc.code == "target_exists":
            raise error(409, "file_exists", "目标文件已存在") from exc
        raise error(500, "metadata_rename_failed", "图片元数据同步失败") from exc
    invalidate_search(request)
    return {
        "meme_id": payload.meme_id,
        "filename": Path(metadata.image.relative_path).name,
        "media_url": f"/media/{payload.meme_id}",
    }


async def delete_image(
    request: Request,
    payload: Any,
    *,
    metadata_service: MetadataServiceProvider,
    acquire_operation: OperationAcquire,
    commit_operation: OperationCommit,
    release_operation: OperationRelease,
    operation_error: OperationErrorProjector,
    invalidate_search: SearchInvalidator,
    error: ErrorFactory,
) -> dict[str, object]:
    """按当前 scope 的稳定 Meme ID 删除图片并收束 operation grant。

    输入是入口已校验的 ``meme_id``；输出只包含稳定 ID 和删除事实。调用场景是图片工作区
    的删除请求，grant 在 metadata durable 副作用前 acquire，成功删除后 commit；只有明确
    未产生 durable 副作用的 metadata 错误才尝试 release，commit 失败不伪造未删除状态。
    """
    if not payload.meme_id:
        raise error(400, "meme_id_required", "必须提供 meme_id")
    scoped_metadata = metadata_service(request)
    try:
        record, _image = scoped_metadata.image_for_meme(payload.meme_id)
    except MetadataError as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc

    try:
        delete_grant = acquire_operation(
            request,
            Operations.IMAGE_DELETE,
            f"delete:{payload.meme_id}:{getattr(record, 'revision', 0)}",
            resource_id=payload.meme_id,
            source="api",
        )
    except OperationPolicyError as exc:
        raise operation_error(exc) from exc

    try:
        scoped_metadata.remove_by_id(payload.meme_id)
    except MetadataError as exc:
        if exc.code == "storage_cleanup_pending":
            # Meme 已在 durable 事务中删除，剩余文件由 storage operation 恢复器继续
            # 清理；grant 必须提交而不能 release，搜索也必须立即失效。原始可恢复
            # 错误仍向客户端返回，避免把“已删除待恢复”伪装成完整成功。
            try:
                commit_operation(request, delete_grant)
            except OperationPolicyError:
                pass
            invalidate_search(request)
            raise error(503, "storage_cleanup_pending", "图片已删除，文件清理待恢复") from exc
        if exc.code in {"meme_not_found", "file_not_found", "target_exists", "invalid_storage_key"}:
            try:
                release_operation(request, delete_grant)
            except OperationPolicyError:
                pass
        raise error(500, exc.code, "图片及其元数据删除失败") from exc
    try:
        commit_operation(request, delete_grant)
    except OperationPolicyError:
        # 删除已经完成，未知 policy 状态交由宿主恢复，不反向报告为未删除。
        pass
    invalidate_search(request)
    return {"meme_id": payload.meme_id, "deleted": True}


__all__ = ["delete_image", "rename_image"]
