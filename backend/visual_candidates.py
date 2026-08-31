"""视觉候选 snapshot 的本地物化实现。

该模块位于任务视觉 snapshot 与 OpenCode workspace provider 之间，仅供开源 local
scope 的默认适配器使用。它从同 scope BlobStore 读取已校验的候选对象，写入 task-scoped
只读目录并生成 manifest；外部 scope 应由宿主 provider 提供等价的物化实现。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from backend.opencode_workspace import (
    ResolvedWorkspace,
    TrustedWorkspaceContext,
    WorkspaceResolutionError,
    validate_directory_path,
    validate_file_path,
)
from backend.visual_snapshot import (
    VisualMatchSnapshotError,
    validate_visual_match_snapshot,
    validate_visual_match_snapshot_manifest,
    visual_match_snapshot_manifest,
)


MAX_CANDIDATE_FILE_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_ROOT_BYTES = 256 * 1024 * 1024


class VisualCandidateMaterializationError(RuntimeError):
    """候选文件无法按 snapshot 身份安全物化时使用的稳定错误。"""

    def __init__(self, code: str = "visual_candidate_materialization_failed") -> None:
        """保存供任务服务识别的稳定错误码。"""
        super().__init__(code)
        self.code = code


def _digest_file(path: Path, *, expected_size: int) -> str:
    """以无跟随符号链接的描述符读取文件并返回 SHA-256。"""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise VisualCandidateMaterializationError() from exc
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size or size > MAX_CANDIDATE_FILE_BYTES:
                raise VisualCandidateMaterializationError()
            digest.update(chunk)
    except OSError as exc:
        raise VisualCandidateMaterializationError() from exc
    finally:
        os.close(descriptor)
    if size != expected_size:
        raise VisualCandidateMaterializationError()
    return digest.hexdigest()


def _copy_identity(source: Path, target: Path, *, sha256: str, size_bytes: int) -> None:
    """复制候选对象并在目标文件上复核大小、SHA 和普通文件类型。"""
    if size_bytes < 0 or size_bytes > MAX_CANDIDATE_FILE_BYTES:
        raise VisualCandidateMaterializationError()
    source_descriptor = -1
    target_descriptor = -1
    try:
        source_info = source.lstat()
        if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode) or source_info.st_size != size_bytes:
            raise VisualCandidateMaterializationError()
        source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        # relative_path 可以包含嵌套目录；逐级 lstat 创建，不能让 mkdir 跟随
        # 预先植入的父级符号链接逃出 task-scoped 临时根。
        validate_directory_path(target.parent, create=True, code="visual_candidate_materialization_failed")
        target_descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(source_descriptor, "rb") as source_handle, os.fdopen(target_descriptor, "wb") as target_handle:
            source_descriptor = -1
            target_descriptor = -1
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    except VisualCandidateMaterializationError:
        raise
    except WorkspaceResolutionError as exc:
        raise VisualCandidateMaterializationError() from exc
    except OSError as exc:
        raise VisualCandidateMaterializationError() from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
    if _digest_file(target, expected_size=size_bytes) != sha256.lower():
        raise VisualCandidateMaterializationError()
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError as exc:
        raise VisualCandidateMaterializationError() from exc


def _read_existing_manifest(path: Path, *, expected_sha256: str) -> dict[str, object]:
    """读取已存在 manifest，确认它仍属于同一个 snapshot。"""
    try:
        validate_file_path(path, code="visual_candidate_materialization_failed")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise VisualCandidateMaterializationError()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(descriptor, 4 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"))
        return validate_visual_match_snapshot_manifest(value, expected_sha256=expected_sha256)
    except VisualCandidateMaterializationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError, VisualMatchSnapshotError) as exc:
        raise VisualCandidateMaterializationError() from exc


def _validate_materialized_tree(root: Path, snapshot: Mapping[str, object]) -> None:
    """确认候选根只包含 manifest 与 snapshot 声明的普通文件。"""
    validate_directory_path(root, code="visual_candidate_materialization_failed")
    root_info = root.lstat()
    if root_info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise VisualCandidateMaterializationError()
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise VisualCandidateMaterializationError()
    declared_bytes = 0
    expected_files = {"manifest.json"}
    expected_directories: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise VisualCandidateMaterializationError()
        relative = candidate.get("relative_path")
        if not isinstance(relative, str):
            raise VisualCandidateMaterializationError()
        size_bytes = candidate.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0 or size_bytes > MAX_CANDIDATE_FILE_BYTES:
            raise VisualCandidateMaterializationError()
        declared_bytes += size_bytes
        if declared_bytes > MAX_CANDIDATE_ROOT_BYTES:
            raise VisualCandidateMaterializationError()
        expected_files.add(relative)
        parts = PurePosixPath(relative).parts
        expected_directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise VisualCandidateMaterializationError()
        if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise VisualCandidateMaterializationError()
        if stat.S_ISDIR(info.st_mode):
            if relative not in expected_directories:
                raise VisualCandidateMaterializationError()
        elif stat.S_ISREG(info.st_mode):
            if relative not in expected_files:
                raise VisualCandidateMaterializationError()
        else:
            raise VisualCandidateMaterializationError()
    total_bytes = 0
    for item in root.rglob("*"):
        if item.is_file():
            total_bytes += item.lstat().st_size
            if total_bytes > MAX_CANDIDATE_ROOT_BYTES:
                raise VisualCandidateMaterializationError()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise VisualCandidateMaterializationError()
        relative = candidate.get("relative_path")
        sha256 = candidate.get("image_sha256")
        size_bytes = candidate.get("size_bytes")
        if not isinstance(relative, str) or not isinstance(sha256, str) or not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            raise VisualCandidateMaterializationError()
        path = root.joinpath(*PurePosixPath(relative).parts)
        validate_file_path(path, code="visual_candidate_materialization_failed")
        if _digest_file(path, expected_size=size_bytes) != sha256.lower():
            raise VisualCandidateMaterializationError()


def _validate_snapshot_sources(resources: Any, context: TrustedWorkspaceContext, snapshot: Mapping[str, object]) -> None:
    """在每次物化或 resume 复用前复核同 scope 候选的当前文件身份。"""
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise VisualCandidateMaterializationError()
    try:
        blob = resources.blob_store_for_scope(context.scope_id)
        with resources.environment(context.scope_id) as environment:
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    raise VisualCandidateMaterializationError()
                meme_id = candidate.get("meme_id")
                image_sha256 = candidate.get("image_sha256")
                size_bytes = candidate.get("size_bytes")
                if (
                    not isinstance(meme_id, str)
                    or not isinstance(image_sha256, str)
                    or not isinstance(size_bytes, int)
                    or isinstance(size_bytes, bool)
                    or size_bytes < 0
                ):
                    raise VisualCandidateMaterializationError()
                meme = environment.memes.get(meme_id)
                if (
                    meme is None
                    or str(meme.sha256).lower() != image_sha256.lower()
                    or int(meme.size_bytes) != size_bytes
                    or not blob.exists_with_identity(meme.storage_key, sha256=image_sha256, size_bytes=size_bytes)
                ):
                    raise VisualCandidateMaterializationError()
    except VisualCandidateMaterializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 源身份复核必须统一失败关闭
        raise VisualCandidateMaterializationError() from exc


def _cleanup_temporary(path: Path) -> None:
    """恢复临时目录写权限后删除，避免只读目录在失败路径残留。"""
    if not (path.exists() or path.is_symlink()):
        return
    try:
        if path.is_symlink():
            path.unlink()
            return
        for item in path.rglob("*"):
            try:
                os.chmod(item, 0o700 if item.is_dir() else 0o600)
            except OSError:
                pass
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def materialize_local_candidates(resources: Any, context: TrustedWorkspaceContext, resolved: ResolvedWorkspace) -> None:
    """将 local scope snapshot 原子物化为 task-scoped 只读候选目录。

    输入是已由任务 claim 校验的 ``context.visual_match_snapshot`` 和 scope-bound
    数据资源；输出是 ``resolved.candidate_root/manifest.json`` 及其候选文件。该函数
    在默认 local provider 中调用，resume 会复用相同 hash 的既有目录。
    """
    raw_snapshot = context.visual_match_snapshot
    if raw_snapshot is None:
        raise VisualCandidateMaterializationError()
    try:
        snapshot = validate_visual_match_snapshot(raw_snapshot)
    except VisualMatchSnapshotError as exc:
        raise VisualCandidateMaterializationError() from exc
    candidates = snapshot.get("candidates")
    if not isinstance(candidates, list):
        raise VisualCandidateMaterializationError()
    declared_bytes = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise VisualCandidateMaterializationError()
        size_bytes = candidate.get("size_bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0 or size_bytes > MAX_CANDIDATE_FILE_BYTES:
            raise VisualCandidateMaterializationError()
        declared_bytes += size_bytes
        if declared_bytes > MAX_CANDIDATE_ROOT_BYTES:
            raise VisualCandidateMaterializationError()
    expected_manifest = visual_match_snapshot_manifest(snapshot)
    expected_sha256 = str(expected_manifest["snapshot_sha256"])
    root = Path(resolved.candidate_root)
    validate_directory_path(root.parent, create=True, code="visual_candidate_materialization_failed")
    _validate_snapshot_sources(resources, context, snapshot)
    if root.exists() or root.is_symlink():
        validate_directory_path(root, code="visual_candidate_materialization_failed")
        existing = _read_existing_manifest(root / "manifest.json", expected_sha256=expected_sha256)
        if existing != expected_manifest:
            raise VisualCandidateMaterializationError()
        _validate_materialized_tree(root, snapshot)
        return

    temporary = root.parent / f".{root.name}.materialize.{uuid.uuid4().hex}"
    validate_directory_path(temporary, allow_missing_leaf=True, code="visual_candidate_materialization_failed")
    try:
        temporary.mkdir(mode=0o700)
        blob = resources.blob_store_for_scope(context.scope_id)
        with resources.environment(context.scope_id) as environment:
            for candidate in snapshot["candidates"]:
                if not isinstance(candidate, Mapping):
                    raise VisualCandidateMaterializationError()
                meme_id = candidate.get("meme_id")
                image_sha256 = candidate.get("image_sha256")
                size_bytes = candidate.get("size_bytes")
                relative_path = candidate.get("relative_path")
                if (
                    not isinstance(meme_id, str)
                    or not isinstance(image_sha256, str)
                    or not isinstance(size_bytes, int)
                    or isinstance(size_bytes, bool)
                    or not isinstance(relative_path, str)
                ):
                    raise VisualCandidateMaterializationError()
                if size_bytes > MAX_CANDIDATE_FILE_BYTES:
                    raise VisualCandidateMaterializationError()
                meme = environment.memes.get(meme_id)
                if meme is None or str(meme.sha256).lower() != image_sha256.lower() or int(meme.size_bytes) != size_bytes:
                    raise VisualCandidateMaterializationError()
                if not blob.exists_with_identity(meme.storage_key, sha256=image_sha256, size_bytes=size_bytes):
                    raise VisualCandidateMaterializationError()
                source = blob.resolve(meme.storage_key)
                target = temporary.joinpath(*PurePosixPath(relative_path).parts)
                _copy_identity(source, target, sha256=image_sha256, size_bytes=size_bytes)
        manifest_path = temporary / "manifest.json"
        raw_manifest = (json.dumps(expected_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(raw_manifest)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for directory in sorted((item for item in temporary.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        try:
            os.rename(temporary, root)
        except FileExistsError:
            validate_directory_path(root, code="visual_candidate_materialization_failed")
            existing = _read_existing_manifest(root / "manifest.json", expected_sha256=expected_sha256)
            if existing != expected_manifest:
                raise VisualCandidateMaterializationError()
            _validate_materialized_tree(root, snapshot)
    except VisualCandidateMaterializationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 适配层只向任务服务暴露稳定错误
        raise VisualCandidateMaterializationError() from exc
    finally:
        if temporary.exists() or temporary.is_symlink():
            _cleanup_temporary(temporary)


__all__ = ["VisualCandidateMaterializationError", "materialize_local_candidates"]
