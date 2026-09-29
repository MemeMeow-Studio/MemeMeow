"""文件对象与 PostgreSQL durable 事实之间的一致性协调模块。

该模块位于持久化资源装配与图片领域服务之间，提供 scope-bound BlobStore 和
StorageCoordinator 的唯一实现；backend.database 只保留历史兼容导出。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.image_naming import content_addressed_key, normalize_display_name, normalize_extension
from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.storage_security import StorageRootError, validate_controlled_root
from backend.persistence.engine import DatabaseError, SCOPE_LOCAL
from backend.persistence.file_lock import shared_file_lock
from backend.persistence.models import DerivedImageThumbnail, Meme, ScopeContext, StorageOperation, Task, utcnow

if TYPE_CHECKING:
    from backend.persistence.resources import DatabaseResources


class BlobStore:
    """绑定 scope 的文件存储；local 使用现有图片根目录，其他 scope 独立命名空间。"""

    def __init__(self, *, root: Path, scope: ScopeContext, storage_namespace: UUID | None = None, local: bool = False):
        """创建受控的 scope 文件根、暂存区和隔离区。

        `root` 是宿主提供的图片或数据根目录，`scope` 固定逻辑数据范围；local
        scope 复用现有图片根，其它 scope 使用不可变 storage namespace。构造失败
        必须抛出稳定 DatabaseError，不能继续使用未校验目录。
        """
        self.scope = scope
        try:
            base_root = validate_controlled_root(root, create=True, writable=True)
            candidate = base_root if local else base_root / "scopes" / str(storage_namespace or uuid.uuid4()) / "images"
            self.root = validate_controlled_root(candidate, create=True, writable=True)
        except StorageRootError as exc:
            raise DatabaseError(str(exc)) from exc
        self.staging_root = self.root / ".staging"
        self.quarantine_root = self.root / ".quarantine"
        try:
            self.staging_root = validate_controlled_root(self.staging_root, create=True, writable=True)
            self.quarantine_root = validate_controlled_root(self.quarantine_root, create=True, writable=True)
            # BlobStore 也可能被离线迁移工具直接构造，确保非 Compose 夹具仍遵守目录契约。
            os.chmod(self.root, 0o700)
            os.chmod(self.staging_root, 0o700)
            os.chmod(self.quarantine_root, 0o700)
        except (StorageRootError, OSError) as exc:
            raise DatabaseError("storage_root_permissions_invalid") from exc

    def resolve(self, storage_key: str, *, must_exist: bool = True) -> Path:
        """安全解析 scope 内相对 key，拒绝绝对路径、穿越和符号链接逃逸。"""
        if not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")) or "\x00" in storage_key:
            raise DatabaseError("invalid_storage_key")
        # 暂存和隔离对象只允许由内部恢复流程访问，不能成为公开 Meme 路径。
        if storage_key == ".staging" or storage_key == ".quarantine" or storage_key.startswith((".staging/", ".quarantine/")):
            raise DatabaseError("internal_storage_key")
        lexical = self.root / storage_key
        current = self.root
        for part in Path(storage_key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        candidate = lexical.resolve()
        # 解析后的不存在目标仍需允许后续上传检查；只有现存路径才要求最终路径在根目录内。
        if candidate == self.root or self.root not in candidate.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not candidate.is_file() or candidate.is_symlink()):
            raise DatabaseError("file_not_found")
        return candidate

    def identity_lock(self, digest: str, extension: str):
        """按当前 scope 的内容身份取得共享文件锁，供全部原图与派生修改入口使用。"""
        key = content_addressed_key(digest, extension)
        return shared_file_lock(self.root / ".locks" / f"{key}.lock")

    def relative(self, path: Path) -> str:
        """返回绑定根目录下的 POSIX storage_key。"""
        candidate = Path(path)
        if candidate.is_symlink():
            raise DatabaseError("symlink_forbidden")
        resolved = candidate.resolve()
        if resolved == self.root or self.root not in resolved.parents:
            raise DatabaseError("path_forbidden")
        return resolved.relative_to(self.root).as_posix()

    def _safe_child(self, base: Path, key: str, *, must_exist: bool = False) -> Path:
        """解析 staging/quarantine 子路径并拒绝符号链接与越界。"""
        if not isinstance(key, str) or not key or key.startswith(("/", "\\")) or "\x00" in key:
            raise DatabaseError("invalid_storage_key")
        lexical = base / key
        current = base
        for part in Path(key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        resolved = lexical.resolve()
        if resolved == base or base not in resolved.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not resolved.is_file() or resolved.is_symlink()):
            raise DatabaseError("file_not_found")
        return resolved

    def stage_bytes(self, content: bytes, *, token: UUID) -> str:
        """以独占创建和 fsync 将已验证字节写入受控暂存区。"""
        key = f"{token.hex}.part"
        target = self._safe_child(self.staging_root, key)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise DatabaseError("staging_conflict") from exc
        except OSError as exc:
            raise DatabaseError("staging_write_failed") from exc
        self._fsync_directory(self.staging_root)
        return f".staging/{key}"

    def _fsync_directory(self, directory: Path) -> None:
        """尽力持久化目录项，平台不支持时保留原子文件语义。"""
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _key_path(self, key: str, *, must_exist: bool = False) -> Path:
        """解析普通 storage key 或内部暂存/隔离 key。"""
        if key.startswith(".staging/"):
            return self._safe_child(self.staging_root, key[len(".staging/"):], must_exist=must_exist)
        if key.startswith(".quarantine/"):
            return self._safe_child(self.quarantine_root, key[len(".quarantine/"):], must_exist=must_exist)
        return self.resolve(key, must_exist=must_exist)

    def link_move(self, source_key: str, target_key: str) -> None:
        """以 link+unlink 实现同文件系统的原子不覆盖移动。"""
        source = self._key_path(source_key, must_exist=True)
        target = self._key_path(target_key, must_exist=False)
        if target.exists() or target.is_symlink():
            raise DatabaseError("target_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target, follow_symlinks=False)
            os.unlink(source)
            self._fsync_directory(target.parent)
            self._fsync_directory(source.parent)
        except FileExistsError as exc:
            raise DatabaseError("target_exists") from exc
        except OSError as exc:
            # 已创建的目标由 durable operation 恢复，错误处理必须保留现有文件。
            raise DatabaseError("file_move_failed") from exc

    def quarantine(self, source_key: str, *, token: UUID) -> str:
        """将图片移动到不可见隔离区并返回隔离 storage key。"""
        target_key = f".quarantine/{token.hex}.blob"
        self.link_move(source_key, target_key)
        return target_key

    def finish_link_move(self, source_key: str, target_key: str) -> bool:
        """恢复 link 已完成的移动；只有两个受控路径指向同一 inode 才删除源路径。"""
        source = self._key_path(source_key, must_exist=True)
        target = self._key_path(target_key, must_exist=True)
        try:
            if source == target or not source.samefile(target):
                return False
            self.unlink(source_key)
            self._fsync_directory(target.parent)
            return True
        except OSError as exc:
            raise DatabaseError("file_move_failed") from exc

    def unlink(self, key: str) -> None:
        """只删除受控普通文件，拒绝符号链接和越界路径。"""
        target = self._key_path(key, must_exist=True)
        if target.is_symlink():
            raise DatabaseError("symlink_forbidden")
        try:
            os.unlink(target)
            self._fsync_directory(target.parent)
        except OSError as exc:
            raise DatabaseError("file_delete_failed") from exc

    def exists_with_identity(self, key: str, *, sha256: str | None = None, size_bytes: int | None = None) -> bool:
        """检查对象存在、非符号链接，并可选复核大小和 SHA。"""
        try:
            path = self._key_path(key, must_exist=True)
            stat = path.stat()
            if size_bytes is not None and stat.st_size != size_bytes:
                return False
            if sha256 is not None:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != sha256:
                    return False
            return True
        except (DatabaseError, OSError):
            return False


class StorageCoordinator:
    """协调 PostgreSQL Meme 记录与 scope-bound 文件存储的可恢复操作。

    上传、重命名和删除都先写入 ``storage_operations``，再执行文件动作；恢复器依据
    记录中的指纹和状态矩阵继续提交、补偿或隔离异常组合，避免中间文件进入正常查询。
    """

    _ACTIVE = {"prepared", "file_applied"}
    _TRANSITIONS = {
        "prepared": {"file_applied", "compensated", "blocked"},
        "file_applied": {"completed", "compensated", "blocked"},
        "completed": set(),
        "compensated": set(),
        "blocked": set(),
    }

    def __init__(self, resources: "DatabaseResources", *, scope_id: str | ScopeContext = SCOPE_LOCAL):
        """创建绑定 scope 的协调器；local 默认仅保留给开源兼容夹具。"""
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(self.scope.scope_id)

    def _set_status(self, operation: StorageOperation, status: str, *, error: dict[str, Any] | None = None, session: Session | None = None) -> None:
        """执行 storage operation 合法状态转移并保存诊断信息。"""
        if status == operation.status:
            return
        if status not in self._TRANSITIONS.get(operation.status, set()):
            raise DatabaseError("invalid_storage_transition")
        operation.status = status
        operation.error = error
        operation.updated_at = utcnow()
        (session or self._session).flush()

    @staticmethod
    def _title_fingerprint(record: Meme) -> str:
        """按自动命名 handler 的规则计算当前语境标题指纹。"""
        context = record.meme_context if isinstance(record.meme_context, dict) else {}
        raw_title = context.get("title") if isinstance(context, dict) else None
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        return hashlib.sha256(title.encode("utf-8")).hexdigest()

    @contextmanager
    def _transaction(self, session: Session | None = None) -> Iterator[Session]:
        """创建当前 scope 的短数据库事务，可选复用已持有身份锁的 Session。"""
        owned_session = session is None
        transaction_session = session if session is not None else self.resources.factory()
        self._session = transaction_session
        try:
            with transaction_session.begin():
                yield transaction_session
        finally:
            self._session = None
            if owned_session:
                transaction_session.close()

    @contextmanager
    def _identity_lock(self, digest: str, extension: str) -> Iterator[Session | None]:
        """在短事务之外持有图片身份锁，等待与文件处理均不申请数据库连接。"""
        with self.blob_store.identity_lock(digest, extension):
            yield None

    @staticmethod
    def _thumbnail_keys(operation: StorageOperation) -> list[str]:
        """读取删除 operation 保存的派生文件 key，并拒绝篡改后的非字符串数据。"""
        raw = getattr(operation, "thumbnail_keys", None)
        if raw is None:
            return []
        if not isinstance(raw, list) or any(not isinstance(key, str) or not key for key in raw):
            raise DatabaseError("thumbnail_cleanup_invalid")
        return list(dict.fromkeys(raw))

    @staticmethod
    def _delete_identity_marker(meme_id: UUID, source_sha256: str, source_size_bytes: int) -> dict[str, object]:
        """构造删除提交后仍可识别 Meme 与同源派生文件的内部 marker。"""
        return {
            "meme_id": str(meme_id),
            "source_sha256": str(source_sha256).lower(),
            "source_size_bytes": source_size_bytes,
        }

    @staticmethod
    def _delete_identity(operation: StorageOperation) -> tuple[UUID, str, int] | None:
        """读取删除 operation 的身份 marker；旧 operation 没有 marker 时返回空。"""
        raw = getattr(operation, "error", None)
        if not isinstance(raw, dict):
            return None
        marker_fields = {"meme_id", "source_sha256", "source_size_bytes"}
        present = marker_fields.intersection(raw)
        if not present:
            return None
        if present != marker_fields:
            raise DatabaseError("delete_identity_invalid")
        try:
            meme_id = UUID(str(raw["meme_id"]))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("delete_identity_invalid") from exc
        source_sha256 = raw["source_sha256"]
        source_size_bytes = raw["source_size_bytes"]
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in source_sha256)
            or not isinstance(source_size_bytes, int)
            or isinstance(source_size_bytes, bool)
            or source_size_bytes < 0
        ):
            raise DatabaseError("delete_identity_invalid")
        return meme_id, source_sha256.lower(), source_size_bytes

    @staticmethod
    def _merge_delete_error(
        marker: dict[str, object] | None,
        *,
        error: str,
        original_pending: bool | None = None,
        thumbnail_count: int | None = None,
    ) -> dict[str, object]:
        """合并删除 marker 与清理诊断，避免待恢复状态覆盖身份事实。"""
        diagnostic = dict(marker or {})
        diagnostic["error"] = error
        if original_pending is not None:
            diagnostic["original_pending"] = original_pending
        if thumbnail_count is not None:
            diagnostic["thumbnail_count"] = thumbnail_count
        return diagnostic

    def _record_delete_cleanup_pending(
        self,
        operation_token: UUID,
        thumbnail_keys: list[str],
        marker: dict[str, object],
        *,
        original_pending: bool,
        error: str,
    ) -> None:
        """在删除已 durable 后尽力保存可重试的清理事实，不重新打开已完成操作。"""
        try:
            with self._transaction() as session:
                current = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == operation_token,
                    )
                    .with_for_update()
                )
                if current is None or current.status == "completed" or current.status not in self._ACTIVE:
                    return
                keys = list(dict.fromkeys([*thumbnail_keys, *self._thumbnail_keys(current)]))
                current.thumbnail_keys = keys
                current.error = self._merge_delete_error(
                    marker,
                    error=error,
                    original_pending=original_pending,
                    thumbnail_count=len(keys),
                )
                current.updated_at = utcnow()
                session.flush()
        except Exception:  # noqa: BLE001 - 原始清理异常仍由恢复扫描依据 durable marker 处理
            return

    def _mark_delete_scan_pending(
        self,
        operation: StorageOperation,
        thumbnail_keys: list[str],
        counts: dict[str, int],
    ) -> None:
        """记录派生目录暂不可扫描的可重试删除状态，保持 operation 活跃。"""
        identity = self._delete_identity(operation)
        marker = self._delete_identity_marker(*identity) if identity is not None else None
        operation.thumbnail_keys = list(dict.fromkeys(thumbnail_keys))
        operation.error = self._merge_delete_error(
            marker,
            error="storage_cleanup_pending",
            thumbnail_count=len(operation.thumbnail_keys),
        )
        operation.updated_at = utcnow()
        counts["retried"] += 1

    @staticmethod
    def _path_present(store: BlobStore, key: str) -> bool:
        """检查受控 key 是否仍有物理对象，包含悬空符号链接等异常状态。"""
        path = store._key_path(key, must_exist=False)
        return path.exists() or path.is_symlink()

    def _cleanup_thumbnail_files(self, keys: list[str]) -> list[str]:
        """幂等清理派生文件并返回本轮仍需重试的 key。"""
        if not keys:
            return []
        try:
            thumbnail_store = self.resources.thumbnail_store_for_scope(self.scope)
        except DatabaseError:
            return list(keys)
        remaining: list[str] = []
        for key in keys:
            try:
                if self._path_present(thumbnail_store, key):
                    thumbnail_store.unlink(key)
            except DatabaseError:
                remaining.append(key)
        return remaining

    def _thumbnail_rows(self, session: Session, meme_id: UUID, *, for_update: bool = True) -> list[DerivedImageThumbnail]:
        """读取当前 Meme 的全部派生事实，删除屏障和恢复器共用。"""
        statement = select(DerivedImageThumbnail).where(
            DerivedImageThumbnail.scope_id == self.scope.scope_id,
            DerivedImageThumbnail.meme_id == meme_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return list(session.scalars(statement))

    def _thumbnail_file_keys(self, meme_id: UUID, source_sha256: str) -> list[str]:
        """按稳定 Meme 和源指纹收集未登记的派生文件 key。"""
        prefix = f"{meme_id.hex}-{str(source_sha256).lower()}-"
        try:
            thumbnail_store = self.resources.thumbnail_store_for_scope(self.scope)
            return [
                path.name
                for path in thumbnail_store.root.iterdir()
                if path.is_file() and not path.is_symlink() and path.name.startswith(prefix)
            ]
        except (DatabaseError, OSError) as exc:
            # 没有完成目录扫描就无法证明不存在孤立派生文件；调用方必须留下可重试
            # 的删除事实，不能把存储不可用误判成“没有文件”。
            raise DatabaseError("thumbnail_storage_unavailable") from exc

    def _thumbnail_keys_for_record(self, session: Session, record: Meme, *, rows: list[DerivedImageThumbnail] | None = None) -> list[str]:
        """收集数据库事实及同源孤立输出，避免删除窗口遗留派生文件。"""
        rows = rows if rows is not None else self._thumbnail_rows(session, record.id)
        keys = [row.output_key for row in rows if row.output_key]
        keys.extend(self._thumbnail_file_keys(record.id, str(record.sha256)))
        return list(dict.fromkeys(keys))

    def _upload_durable(
        self,
        content: bytes,
        *,
        digest: str,
        normalized_extension: str,
        normalized_display_name: str,
        context: dict[str, Any],
        provenance: dict[str, Any],
        status: str,
        extensions: dict[str, Any] | None,
        meme_id: UUID | None,
        lock_session: Session | None,
    ) -> Meme:
        """在内容身份锁内完成 Meme、文件和 storage operation 的 durable 收束。"""
        physical_key = content_addressed_key(digest, normalized_extension)
        target = self.blob_store._key_path(physical_key)
        token = uuid.uuid4()
        staging_key: str | None = None
        try:
            # 暂存先于数据库写入，避免把请求缓冲直接暴露为可见文件；身份锁会让并发
            # 请求在首个请求完成文件落位后再读取 durable 事实。
            if target.exists() or target.is_symlink():
                if not self.blob_store.exists_with_identity(physical_key, sha256=digest, size_bytes=len(content)):
                    raise DatabaseError("upload_reconciliation_required")
                with self._transaction(lock_session) as session:
                    existing = session.scalar(
                        select(Meme)
                        .where(
                            Meme.scope_id == self.scope.scope_id,
                            Meme.sha256 == digest,
                            Meme.extension == normalized_extension,
                        )
                        .with_for_update()
                    )
                    if existing is None or existing.storage_key != physical_key:
                        raise DatabaseError("upload_reconciliation_required")
                    return existing

            staging_key = self.blob_store.stage_bytes(content, token=token)
            with self._transaction(lock_session) as session:
                existing = session.scalar(
                    select(Meme)
                    .where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.sha256 == digest,
                        Meme.extension == normalized_extension,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    raise DatabaseError("upload_reconciliation_required")

                from backend.metadata import MemeContext
                from backend.embedding_input import build_text_input

                parsed_context = MemeContext.model_validate(context)
                text_input = build_text_input(normalized_display_name, parsed_context, status)
                record = Meme(
                    id=meme_id or uuid.uuid4(),
                    scope_id=self.scope.scope_id,
                    storage_key=physical_key,
                    display_name=normalized_display_name,
                    name_source="uploaded" if provenance.get("upload_receipt_id") else "unknown",
                    text_input_status=text_input.status,
                    extension=normalized_extension,
                    size_bytes=len(content),
                    sha256=digest,
                    context_status=status,
                    search_metadata_hash=text_input.fingerprint,
                    meme_context=parsed_context.model_dump(mode="json", exclude_none=False),
                    provenance=provenance,
                    extensions=dict(extensions or {}),
                    revision=1,
                )
                session.add(record)
                session.flush()
                session.add(
                    StorageOperation(
                        scope_id=self.scope.scope_id,
                        meme_id=record.id,
                        operation_type="upload",
                        operation_token=token,
                        target_key=physical_key,
                        staging_key=staging_key,
                        after_sha256=digest,
                        after_size=len(content),
                        status="prepared",
                    )
                )
                session.flush()

            self.blob_store.link_move(staging_key, physical_key)
            with self._transaction(lock_session) as session:
                operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
                if operation is None:
                    raise DatabaseError("storage_operation_missing")
                self._session = session
                self._set_status(operation, "file_applied", session=session)
                self._set_status(operation, "completed", session=session)
            return record
        except IntegrityError as exc:
            # 内容唯一键可能在本事务之外刚刚被并发请求提交；HTTP 层会重新读取
            # by_content 事实并把已完成写入收束成幂等成功。
            raise DatabaseError("target_exists") from exc
        except Exception:
            # 暂存文件没有数据库引用时可以安全清理；已写入 operation 的异常留给恢复器。
            if staging_key is not None:
                with self._transaction() as session:
                    referenced = session.scalar(select(StorageOperation.id).where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    ))
                if referenced is None and self.blob_store.exists_with_identity(staging_key, sha256=digest, size_bytes=len(content)):
                    self.blob_store.unlink(staging_key)
            raise

    def upload(
        self,
        content: bytes,
        *,
        target_key: str | None = None,
        extension: str | None = None,
        display_name: str | None = None,
        context: dict[str, Any],
        provenance: dict[str, Any],
        status: str = "pending",
        extensions: dict[str, Any] | None = None,
        meme_id: UUID | None = None,
    ) -> Meme:
        """以内容寻址 key durable 写入上传，并按内容身份实现 scope 内幂等。

        `target_key` 保留为旧调用方传入的用户文件名；它只用于提取展示名称和扩展名，
        不再作为物理路径。最终 SHA、扩展名和展示名在同一事务中确定，文件操作仍通过
        `StorageOperation` 记录，供并发上传和进程崩溃后的恢复器收束。`status` 和
        `extensions` 仅供受控迁移保留已验证语境，普通上传使用默认值。
        """
        requested_key = target_key
        if requested_key is not None:
            try:
                validate_business_storage_key(requested_key)
            except ValueError as exc:
                raise DatabaseError(str(exc)) from exc
        try:
            normalized_extension = normalize_extension(extension or (Path(requested_key).suffix if requested_key else None))
            normalized_display_name = normalize_display_name(
                display_name if display_name is not None else Path(requested_key).stem if requested_key else "image"
            )
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        if status not in {"pending", "partial", "ready", "repair_required"}:
            raise DatabaseError("invalid_context_status")
        if extensions is not None and not isinstance(extensions, dict):
            raise DatabaseError("extensions_invalid")

        digest = hashlib.sha256(content).hexdigest()
        with self._identity_lock(digest, normalized_extension) as lock_session:
            return self._upload_durable(
                content,
                digest=digest,
                normalized_extension=normalized_extension,
                normalized_display_name=normalized_display_name,
                context=context,
                provenance=provenance,
                status=status,
                extensions=extensions,
                meme_id=meme_id,
                lock_session=lock_session,
            )

    def rename(self, meme_id: UUID | str, *, target_key: str | None = None, display_name: str | None = None) -> Meme:
        """只更新 Meme 展示名称，保留不可变物理 key 和图片 revision。"""
        if display_name is None:
            if target_key is None:
                raise DatabaseError("display_name_required")
            try:
                validate_business_storage_key(target_key)
            except ValueError as exc:
                raise DatabaseError(str(exc)) from exc
            display_name = Path(target_key).stem
        try:
            normalized_name = normalize_display_name(display_name)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        try:
            identifier = UUID(str(meme_id))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("meme_not_found") from exc
        with self._transaction() as session:
            record = session.scalar(
                select(Meme)
                .where(Meme.scope_id == self.scope.scope_id, Meme.id == identifier)
                .with_for_update()
            )
            if record is None:
                raise DatabaseError("meme_not_found")
            from backend.persistence.repositories.memes import MemeRepository

            return MemeRepository(session, self.scope).update_display_name(record.id, normalized_name)

    def _rename_if_current_display_name(
        self,
        meme_id: UUID | str,
        *,
        target_key: str,
        expected_source_key: str,
        expected_sha256: str,
        expected_revision: int,
        task_id: str,
        claim_generation: int,
        attempt: int,
        claim_owner: str,
        expected_title_fingerprint: str | None = None,
        expected_display_name: str | None = None,
    ) -> Meme:
        """在自动命名 claim 内只提交展示名，保留图片物理身份不变。

        `expected_display_name` 是任务创建时观察到的用户可见名称；提供时会一并
        参与 CAS，用于防止排队期间的人工重命名被自动命名覆盖。历史调用方省略
        该字段时仍保留原有图片身份校验。
        """
        try:
            validate_business_storage_key(target_key)
            validate_business_storage_key(expected_source_key)
        except ValueError as exc:
            raise DatabaseError("invalid_filename") from exc
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(claim_owner, str)
            or not claim_owner
            or type(expected_revision) is not int
            or expected_revision < 1
            or type(claim_generation) is not int
            or claim_generation < 1
            or type(attempt) is not int
            or attempt < 1
        ):
            raise DatabaseError("target_changed")
        if Path(target_key).suffix.lower() != Path(expected_source_key).suffix.lower():
            raise DatabaseError("invalid_filename")
        try:
            normalized_name = normalize_display_name(Path(target_key).stem)
            identifier = UUID(str(meme_id))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("target_changed") from exc
        if expected_display_name is not None:
            try:
                expected_display_name = normalize_display_name(expected_display_name)
            except ValueError as exc:
                raise DatabaseError("target_changed") from exc
        with self.resources.environment(self.scope) as environment:
            snapshot = environment.memes.get(identifier)
        if snapshot is None or not self.blob_store.exists_with_identity(
            snapshot.storage_key, sha256=snapshot.sha256, size_bytes=snapshot.size_bytes,
        ):
            raise DatabaseError("target_changed")
        with self._transaction() as session:
            record = session.scalar(
                select(Meme)
                .where(Meme.scope_id == self.scope.scope_id, Meme.id == identifier)
                .with_for_update()
            )
            task = session.scalar(
                select(Task)
                .where(Task.scope_id == self.scope.scope_id, Task.id == task_id)
                .with_for_update()
            )
            now = utcnow()
            if (
                task is None
                or task.task_type != "image_auto_rename"
                or task.image_stage != "auto_rename"
                or task.status != "running"
                or task.claim_generation != claim_generation
                or task.attempt_count != attempt
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
                or task.lease_owner != claim_owner
            ):
                raise DatabaseError("claim_expired")
            if record is None or record.sha256.lower() != expected_sha256.lower():
                raise DatabaseError("target_changed")
            if expected_title_fingerprint is not None and self._title_fingerprint(record) != expected_title_fingerprint:
                raise DatabaseError("target_changed")
            if record.storage_key != expected_source_key:
                raise DatabaseError("storage_key_changed")
            if expected_display_name is not None and record.display_name != expected_display_name:
                raise DatabaseError("display_name_changed")
            if record.revision != expected_revision:
                raise DatabaseError("target_changed")
            if (record.storage_key, record.sha256, record.size_bytes) != (snapshot.storage_key, snapshot.sha256, snapshot.size_bytes):
                raise DatabaseError("target_changed")
            unsettled_operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.meme_id == record.id,
                    StorageOperation.status.in_(("prepared", "file_applied", "blocked")),
                )
                .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.desc())
                .with_for_update()
            )
            if unsettled_operation is not None:
                raise DatabaseError("storage_operation_unknown")
            from backend.persistence.repositories.memes import MemeRepository

            return MemeRepository(session, self.scope).update_display_name(
                record.id, normalized_name, name_source="automatic",
                expected_display_name=expected_display_name,
                expected_revision=expected_revision, expected_sha256=expected_sha256,
                claim=(task_id, claim_generation, claim_owner),
            )

    def rename_if_current(
        self,
        meme_id: UUID | str,
        *,
        target_key: str,
        expected_source_key: str,
        expected_sha256: str,
        expected_revision: int,
        task_id: str,
        claim_generation: int,
        attempt: int,
        claim_owner: str,
        expected_title_fingerprint: str | None = None,
        expected_display_name: str | None = None,
    ) -> Meme:
        """在任务 claim 与 Meme 事实仍匹配时执行一次展示名 CAS 更新。

        自动命名不再移动物理文件，也不改变 ``storage_key``、图片 SHA 或图片 revision。
        仍需复核任务租约、当前图片身份、源 key、标题指纹和 revision，避免过期任务
        覆盖人工或并发更新；实际文件操作只保留给历史恢复器处理旧 operation。
        """
        return self._rename_if_current_display_name(
            meme_id,
            target_key=target_key,
            expected_source_key=expected_source_key,
            expected_sha256=expected_sha256,
            expected_revision=expected_revision,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            claim_owner=claim_owner,
            expected_title_fingerprint=expected_title_fingerprint,
            expected_display_name=expected_display_name,
        )
    def delete(self, meme_id: UUID | str) -> None:
        """读取内容身份后在共享文件锁内完成删除，等待期间不持有事务。"""
        with self.resources.environment(self.scope) as environment:
            snapshot = environment.memes.get(meme_id)
            if snapshot is None:
                raise DatabaseError("meme_not_found")
        with self.blob_store.identity_lock(snapshot.sha256, snapshot.extension):
            self._delete_locked(meme_id, snapshot)

    def _delete_locked(self, meme_id: UUID | str, snapshot: Meme) -> None:
        """先阻断派生访问并隔离原图，再删除 Meme 记录和派生对象。"""
        token = uuid.uuid4()
        thumbnail_keys: list[str] = []
        delete_marker: dict[str, object] | None = None
        delete_identity: tuple[UUID, str, int] | None = None
        before_sha256: str | None = None
        before_size: int | None = None
        if not self.blob_store.exists_with_identity(snapshot.storage_key, sha256=snapshot.sha256, size_bytes=snapshot.size_bytes):
            raise DatabaseError("target_changed")
        file_keys = self._thumbnail_file_keys(snapshot.id, snapshot.sha256)
        with self._transaction() as session:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            if (record.storage_key, record.sha256, record.size_bytes) != (snapshot.storage_key, snapshot.sha256, snapshot.size_bytes):
                raise DatabaseError("target_changed")
            before_sha256 = str(record.sha256).lower()
            before_size = record.size_bytes
            delete_identity = (record.id, before_sha256, before_size)
            delete_marker = self._delete_identity_marker(*delete_identity)
            thumbnail_rows = self._thumbnail_rows(session, record.id)
            thumbnail_keys = list(dict.fromkeys([*file_keys, *(row.output_key for row in thumbnail_rows if row.output_key)]))
            for row in thumbnail_rows:
                # 删除操作进入 durable prepared 后，任何旧缩略图都必须立即失效；
                # output_key 保留到事务完成后供清理器回收物理文件。
                row.status = "stale"
                row.diagnostic = {"error": "meme_delete_in_progress"}
                row.updated_at = utcnow()
            operation = StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="delete", operation_token=token, source_key=record.storage_key, target_key=f".quarantine/{token.hex}.blob", before_sha256=before_sha256, before_size=before_size, thumbnail_keys=thumbnail_keys, error=delete_marker, status="prepared")
            session.add(operation)
            session.flush()
            source_key = record.storage_key
        self.blob_store.quarantine(source_key, token=token)
        with self._transaction() as session:
            operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.operation_token == token,
                )
                .with_for_update()
            )
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if operation is None or record is None:
                raise DatabaseError("storage_operation_missing")
            # 共享图片锁覆盖文件操作，最终事务仍核对数据库派生记录。
            thumbnail_rows = self._thumbnail_rows(session, record.id)
            thumbnail_keys = list(dict.fromkeys([*(row.output_key for row in thumbnail_rows if row.output_key), *self._thumbnail_keys(operation)]))
            operation.thumbnail_keys = thumbnail_keys
            for row in thumbnail_rows:
                row.status = "stale"
                row.diagnostic = {"error": "meme_delete_in_progress"}
                row.updated_at = utcnow()
            self._session = session
            self._set_status(operation, "file_applied", session=session)
            # Meme 外键解除后仍保留稳定身份；生成 Worker 崩溃时恢复器可按 marker
            # 扫描首次快照后才落位、且未写入派生表的物理输出。
            operation.error = delete_marker
            operation.updated_at = utcnow()
            # 删除 Meme 前解除关联，保留 file_applied operation 作为未完成清理的恢复事实。
            operation.meme_id = None
            session.delete(record)
            session.flush()
        cleanup_target = f".quarantine/{token.hex}.blob"
        original_pending = False
        remaining = list(thumbnail_keys)
        cleanup_already_completed = False
        try:
            assert delete_identity is not None and before_sha256 is not None and before_size is not None and delete_marker is not None
            original_pending = not self.blob_store.exists_with_identity(cleanup_target, sha256=before_sha256, size_bytes=before_size)
            if not original_pending:
                try:
                    self.blob_store.unlink(cleanup_target)
                except DatabaseError:
                    original_pending = True
            remaining = self._cleanup_thumbnail_files(thumbnail_keys)
            try:
                late_thumbnail_keys = self._thumbnail_file_keys(delete_identity[0], delete_identity[1])
            except DatabaseError:
                # 目录扫描失败时由下面的异常收束路径保留 marker 和已知 key；恢复器
                # 下一次运行会在存储可用后重新扫描同一源版本。
                raise
            if late_thumbnail_keys:
                remaining = self._cleanup_thumbnail_files(list(dict.fromkeys([*remaining, *late_thumbnail_keys])))
            with self._transaction() as session:
                current = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
                if current is None:
                    raise DatabaseError("storage_operation_missing")
                if current.status == "completed":
                    cleanup_already_completed = True
                elif current.status not in self._ACTIVE:
                    raise DatabaseError("storage_operation_unknown")
                elif original_pending or remaining:
                    current.thumbnail_keys = remaining
                    current.error = self._merge_delete_error(
                        delete_marker,
                        error="storage_cleanup_pending",
                        original_pending=original_pending,
                        thumbnail_count=len(remaining),
                    )
                    current.updated_at = utcnow()
                    session.flush()
                else:
                    current.thumbnail_keys = []
                    self._set_status(current, "completed", session=session)
        except (DatabaseError, OSError) as exc:
            # 原图隔离和 Meme 删除已经提交；第三阶段失败只能留下可重试的
            # file_applied 事实，不能让 HTTP 层把该操作误判为可 release。
            self._record_delete_cleanup_pending(
                token,
                remaining,
                delete_marker or {},
                original_pending=original_pending,
                error="storage_cleanup_pending",
            )
            raise DatabaseError("storage_cleanup_pending") from exc
        if cleanup_already_completed:
            return
        if original_pending or remaining:
            raise DatabaseError("storage_cleanup_pending")

    def recover(self, *, limit: int = 100) -> dict[str, int]:
        """逐项在图片身份锁内恢复操作，每项文件处理之前归还查询连接。"""
        counts = {"completed": 0, "compensated": 0, "blocked": 0, "retried": 0}
        with self.resources.factory() as session:
            rows = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE))).order_by(StorageOperation.updated_at).limit(max(1, min(limit, 1000)))))
        for snapshot in rows:
            if snapshot.operation_type not in {"upload", "delete", "rename"}:
                with self._transaction() as session:
                    operation = self._recovery_current(session, snapshot)
                    self._set_status(operation, "blocked", error={"error": "storage_operation_unknown_type"}, session=session)
                    counts["blocked"] += 1
                continue
            digest = snapshot.after_sha256 if snapshot.operation_type == "upload" else snapshot.before_sha256
            key = snapshot.target_key if snapshot.operation_type == "upload" else snapshot.source_key
            with self.blob_store.identity_lock(digest, Path(key).suffix):
                with self.resources.factory() as session:
                    operation = session.get(StorageOperation, snapshot.id)
                    if operation is None or operation.status not in self._ACTIVE:
                        continue
                    # 记录保持已加载状态，后续文件操作不会触发隐式查询。
                    session.expunge(operation)
                if operation.operation_type in {"upload", "delete"}:
                    self._recover_file_operation(operation, counts)
                    continue
                try:
                    self._recover_rename(operation, counts)
                except DatabaseError as exc:
                    self._record_recovery_error(operation, counts, exc)
        return counts

    def _recover_file_operation(self, snapshot: StorageOperation, counts: dict[str, int]) -> None:
        """在共享锁内处理上传或删除文件，再以短事务核对 operation 并提交状态。"""
        try:
            with self.resources.environment(self.scope) as environment:
                record = environment.memes.get(snapshot.meme_id) if snapshot.meme_id else None
                if record is not None:
                    expected = (snapshot.after_sha256, snapshot.after_size, snapshot.target_key) if snapshot.operation_type == "upload" else (snapshot.before_sha256, snapshot.before_size, snapshot.source_key)
                    if (record.sha256, record.size_bytes, record.storage_key) != expected:
                        raise DatabaseError("storage_recovery_target_changed")
            if snapshot.operation_type == "upload":
                target_ok = self.blob_store.exists_with_identity(snapshot.target_key, sha256=snapshot.after_sha256, size_bytes=snapshot.after_size)
                stage_ok = self.blob_store.exists_with_identity(snapshot.staging_key, sha256=snapshot.after_sha256, size_bytes=snapshot.after_size)
                if (not target_ok and self._path_present(self.blob_store, snapshot.target_key)) or (not stage_ok and self._path_present(self.blob_store, snapshot.staging_key)):
                    raise DatabaseError("upload_blob_identity_mismatch")
                if target_ok and stage_ok and self.blob_store.finish_link_move(snapshot.staging_key, snapshot.target_key):
                    stage_ok = False
                    counts["retried"] += 1
                if snapshot.status == "prepared" and stage_ok and not target_ok:
                    self.blob_store.link_move(snapshot.staging_key, snapshot.target_key)
                    target_ok, stage_ok = True, False
                    counts["retried"] += 1
                if stage_ok:
                    raise DatabaseError("upload_recovery_ambiguous")
                with self._transaction() as session:
                    operation = self._recovery_current(session, snapshot)
                    if target_ok:
                        self._set_status(operation, "file_applied", session=session)
                        self._set_status(operation, "completed", session=session)
                        counts["completed"] += 1
                    else:
                        record = session.get(Meme, operation.meme_id) if operation.meme_id else None
                        if record is not None:
                            if record.scope_id != self.scope.scope_id or (record.sha256, record.size_bytes, record.storage_key) != (snapshot.after_sha256, snapshot.after_size, snapshot.target_key):
                                raise DatabaseError("upload_target_changed")
                            operation.meme_id = None
                            session.delete(record)
                        self._set_status(operation, "compensated", session=session)
                        counts["compensated"] += 1
                return
            identity = self._delete_identity(snapshot)
            identifier = identity[0] if identity is not None else snapshot.meme_id
            if identity is not None and snapshot.meme_id is not None and snapshot.meme_id != identifier:
                raise DatabaseError("delete_identity_mismatch")
            if identity is not None and (identity[1], identity[2]) != (snapshot.before_sha256, snapshot.before_size):
                raise DatabaseError("delete_identity_mismatch")
            keys = self._thumbnail_keys(snapshot)
            if identifier is not None:
                keys.extend(self._thumbnail_file_keys(identifier, snapshot.before_sha256))
            source_ok = self.blob_store.exists_with_identity(snapshot.source_key, sha256=snapshot.before_sha256, size_bytes=snapshot.before_size)
            quarantine_ok = self.blob_store.exists_with_identity(snapshot.target_key, sha256=snapshot.before_sha256, size_bytes=snapshot.before_size)
            if source_ok and quarantine_ok and self.blob_store.finish_link_move(snapshot.source_key, snapshot.target_key):
                source_ok = False
                counts["retried"] += 1
            if snapshot.status == "prepared" and source_ok and not quarantine_ok:
                self.blob_store.quarantine(snapshot.source_key, token=snapshot.operation_token)
                source_ok, quarantine_ok = False, True
                counts["retried"] += 1
            if source_ok or (snapshot.status == "prepared" and not quarantine_ok):
                raise DatabaseError("delete_recovery_ambiguous")
            if not quarantine_ok and (snapshot.meme_id is not None or self._path_present(self.blob_store, snapshot.target_key)):
                raise DatabaseError("delete_blob_missing")
            with self._transaction() as session:
                operation = self._recovery_current(session, snapshot)
                record = session.get(Meme, identifier, with_for_update=True) if identifier else None
                if record is not None:
                    if record.scope_id != self.scope.scope_id or snapshot.meme_id is None or (record.sha256, record.size_bytes, record.storage_key) != (snapshot.before_sha256, snapshot.before_size, snapshot.source_key):
                        raise DatabaseError("delete_target_changed")
                    keys.extend(row.output_key for row in self._thumbnail_rows(session, record.id) if row.output_key)
                    operation.meme_id = None
                    session.delete(record)
                self._set_status(operation, "file_applied", session=session)
                operation.error = self._delete_identity_marker(*identity) if identity else snapshot.error
                operation.thumbnail_keys = list(dict.fromkeys(keys))
                session.flush()
            if quarantine_ok:
                self.blob_store.unlink(snapshot.target_key)
            elif self._path_present(self.blob_store, snapshot.target_key):
                raise DatabaseError("delete_blob_identity_mismatch")
            remaining = self._cleanup_thumbnail_files(keys)
            with self._transaction() as session:
                operation = session.get(StorageOperation, snapshot.id, with_for_update=True)
                if operation is None or operation.status != "file_applied":
                    raise DatabaseError("storage_operation_changed")
                operation.thumbnail_keys = remaining
                if remaining:
                    operation.error = self._merge_delete_error(self._delete_identity_marker(*identity) if identity else None, error="storage_cleanup_pending", thumbnail_count=len(remaining))
                    counts["retried"] += 1
                else:
                    self._set_status(operation, "completed", session=session)
                    counts["completed"] += 1
        except DatabaseError as exc:
            self._record_recovery_error(snapshot, counts, exc)

    def _record_recovery_error(self, snapshot: StorageOperation, counts: dict[str, int], error: DatabaseError) -> None:
        """保存恢复错误的具体原因，文件系统暂时错误保留可重试状态。"""
        with self._transaction() as session:
            operation = session.get(StorageOperation, snapshot.id, with_for_update=True)
            if operation is None or operation.status not in self._ACTIVE:
                raise error
            diagnostic = dict(operation.error or {})
            diagnostic.update(error=error.code, message=str(error))
            if error.code in {"thumbnail_storage_unavailable", "file_delete_failed", "file_move_failed"}:
                operation.error = diagnostic
                operation.updated_at = utcnow()
                counts["retried"] += 1
            else:
                self._set_status(operation, "blocked", error=diagnostic, session=session)
                counts["blocked"] += 1

    def _recovery_current(self, session: Session, snapshot: StorageOperation) -> StorageOperation:
        """在文件处理后锁定并复核操作版本，拒绝使用已经变化的恢复输入。"""
        operation = session.get(StorageOperation, snapshot.id, with_for_update=True)
        if operation is None or operation.status != snapshot.status or operation.updated_at != snapshot.updated_at:
            raise DatabaseError("storage_operation_changed")
        return operation

    def _recover_upload(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复上传暂存文件或补偿无文件的 Meme。"""
        assert operation.target_key and operation.staging_key
        target_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        stage_ok = self.blob_store.exists_with_identity(operation.staging_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        if operation.status == "prepared" and stage_ok and not target_ok:
            self.blob_store.link_move(operation.staging_key, operation.target_key)
            stage_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not stage_ok:
            self._set_status(operation, "file_applied", session=session)
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif not target_ok and not stage_ok:
            record = session.get(Meme, operation.meme_id) if operation.meme_id else None
            if record is not None:
                operation.meme_id = None
                self._session.delete(record)
            self._set_status(operation, "compensated", session=session)
            counts["compensated"] += 1
        else:
            raise DatabaseError("upload_recovery_ambiguous")

    def _rename_recovery_record(self, session: Session, operation: StorageOperation) -> tuple[Meme, bool, bool]:
        """复核历史重命名的任务及记录版本，返回源绑定与已提交状态。"""
        record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
        if record is None:
            raise DatabaseError("meme_not_found")
        if operation.task_id is not None:
            # 自动重命名的恢复必须仍属于创建 operation 时的叶子 claim。任务被
            # 重新认领、完成或租约过期后，不能让恢复器继续移动或 finalize 文件。
            task = session.scalar(
                select(Task)
                .where(Task.scope_id == self.scope.scope_id, Task.id == operation.task_id)
                .with_for_update()
            )
            now = utcnow()
            if (
                task is None
                or task.task_type != "image_auto_rename"
                or task.image_stage != "auto_rename"
                or task.status != "running"
                or operation.claim_generation is None
                or task.claim_generation != operation.claim_generation
                or operation.attempt is None
                or task.attempt_count != operation.attempt
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
            ):
                raise DatabaseError("rename_claim_expired")
        if (
            not isinstance(operation.before_sha256, str)
            or len(operation.before_sha256) != 64
            or not isinstance(operation.after_sha256, str)
            or len(operation.after_sha256) != 64
        ):
            raise DatabaseError("rename_operation_invalid")
        same_image = record.sha256.lower() == operation.before_sha256.lower()
        title_matches = operation.expected_title_fingerprint is None or self._title_fingerprint(record) == operation.expected_title_fingerprint
        already_finalized = (
            record.storage_key == operation.target_key
            and operation.expected_revision is not None
            and record.revision == operation.expected_revision + 1
        )
        source_binding = (
            record.storage_key == operation.source_key
            and (operation.expected_revision is None or record.revision == operation.expected_revision)
        )
        if not same_image or not title_matches:
            raise DatabaseError("rename_target_changed")
        return record, source_binding, already_finalized

    def _recover_rename(self, snapshot: StorageOperation, counts: dict[str, int]) -> None:
        """在事务外恢复历史文件移动，前后短事务均复核任务与记录版本。"""
        assert snapshot.source_key and snapshot.target_key
        source_ok = self.blob_store.exists_with_identity(snapshot.source_key, sha256=snapshot.before_sha256, size_bytes=snapshot.before_size)
        target_ok = self.blob_store.exists_with_identity(snapshot.target_key, sha256=snapshot.after_sha256, size_bytes=snapshot.after_size)
        with self._transaction() as session:
            operation = self._recovery_current(session, snapshot)
            _record, source_binding, already_finalized = self._rename_recovery_record(session, operation)
            if operation.status == "prepared" and not source_binding and not already_finalized:
                if not source_ok and not target_ok:
                    self._set_status(operation, "compensated", error={"error": "storage_key_changed"}, session=session)
                    counts["compensated"] += 1
                    return
                raise DatabaseError("rename_target_changed")
        if source_ok and target_ok and self.blob_store.finish_link_move(snapshot.source_key, snapshot.target_key):
            source_ok = False
            counts["retried"] += 1
        if snapshot.status == "prepared" and source_ok and not target_ok:
            self.blob_store.link_move(snapshot.source_key, snapshot.target_key)
            source_ok, target_ok = False, True
            counts["retried"] += 1
        if not target_ok or source_ok:
            raise DatabaseError("rename_recovery_ambiguous")
        with self._transaction() as session:
            operation = self._recovery_current(session, snapshot)
            record, source_binding, already_finalized = self._rename_recovery_record(session, operation)
            if already_finalized:
                self._set_status(operation, "file_applied", session=session)
            elif source_binding:
                self._set_status(operation, "file_applied", session=session)
                record.storage_key = operation.target_key
                record.revision += 1
                record.updated_at = utcnow()
            else:
                raise DatabaseError("rename_target_changed")
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1

    def _recover_delete(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复原图隔离和派生清理；冲突时阻断自动修改。"""
        assert operation.source_key and operation.target_key
        thumbnail_keys = self._thumbnail_keys(operation)
        delete_identity = self._delete_identity(operation)
        if delete_identity is not None:
            marker_meme_id, marker_sha256, marker_size = delete_identity
            if operation.meme_id is not None and UUID(str(operation.meme_id)) != marker_meme_id:
                raise DatabaseError("delete_identity_mismatch")
            if (
                str(operation.before_sha256).lower() != marker_sha256
                or operation.before_size != marker_size
            ):
                raise DatabaseError("delete_identity_mismatch")
            # Meme 外键解除后只能依靠 marker 定位同一 Meme 的未登记派生输出；
            # 其 key 仍包含稳定 ID 和源 SHA，不接受任意文件名扫描。
            try:
                thumbnail_keys.extend(self._thumbnail_file_keys(marker_meme_id, marker_sha256))
            except DatabaseError as exc:
                if exc.code != "thumbnail_storage_unavailable":
                    raise
                self._mark_delete_scan_pending(operation, thumbnail_keys, counts)
                return
            thumbnail_keys = list(dict.fromkeys(thumbnail_keys))
            if operation.meme_id is None:
                replacement = session.scalar(
                    select(Meme)
                    .where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.id == marker_meme_id,
                    )
                    .with_for_update()
                )
                if replacement is not None:
                    raise DatabaseError("delete_target_changed")
        source_ok = self.blob_store.exists_with_identity(operation.source_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        quarantine_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        if operation.status == "prepared" and source_ok and not quarantine_ok:
            self.blob_store.quarantine(operation.source_key, token=UUID(operation.operation_token.hex))
            source_ok, quarantine_ok = False, True
            counts["retried"] += 1
        if operation.status == "prepared" and quarantine_ok and not source_ok:
            self._set_status(operation, "file_applied", session=session)
            if delete_identity is not None:
                operation.error = self._delete_identity_marker(*delete_identity)
                operation.updated_at = utcnow()
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update()) if operation.meme_id else None
            if record is not None:
                if (
                    record.storage_key != operation.source_key
                    or record.sha256.lower() != str(operation.before_sha256).lower()
                    or record.size_bytes != operation.before_size
                ):
                    # prepared 期间原图已隔离但删除事务尚未提交；若 Meme 已被其它
                    # 操作改写，不能把当前记录误当成原删除目标强行删除。
                    raise DatabaseError("delete_target_changed")
                thumbnail_rows = self._thumbnail_rows(session, record.id)
                try:
                    thumbnail_keys = list(dict.fromkeys([*thumbnail_keys, *self._thumbnail_keys_for_record(session, record, rows=thumbnail_rows)]))
                except DatabaseError as exc:
                    if exc.code != "thumbnail_storage_unavailable":
                        raise
                    self._mark_delete_scan_pending(operation, thumbnail_keys, counts)
                    return
                operation.thumbnail_keys = thumbnail_keys
                for row in thumbnail_rows:
                    row.status = "stale"
                    row.diagnostic = {"error": "meme_delete_in_progress"}
                    row.updated_at = utcnow()
                operation.meme_id = None
                self._session.delete(record)
            elif operation.meme_id is not None:
                # Meme 记录可能已由前一次恢复提交删除，但 operation 仍保留旧 ID；
                # 在解除外键前仍可按同一源指纹回收未登记输出。
                try:
                    thumbnail_keys = list(
                        dict.fromkeys(
                            [
                                *thumbnail_keys,
                                *self._thumbnail_file_keys(operation.meme_id, str(operation.before_sha256)),
                            ]
                        )
                    )
                except DatabaseError as exc:
                    if exc.code != "thumbnail_storage_unavailable":
                        raise
                    self._mark_delete_scan_pending(operation, thumbnail_keys, counts)
                    return
                operation.thumbnail_keys = thumbnail_keys
            session.flush()
        elif operation.status == "prepared":
            raise DatabaseError("delete_recovery_ambiguous")
        elif source_ok:
            raise DatabaseError("delete_recovery_ambiguous")

        if operation.status == "file_applied" and operation.meme_id is not None:
            # file_applied 只证明原图已隔离，不证明删除 Meme 的事务已经提交；恢复时
            # 必须再次核对待删记录的原图身份，再解除 operation 外键后删除，避免残留
            # Meme 指向已不存在的原图，也避免误删后来写入的同 ID 记录。
            record = session.scalar(
                select(Meme)
                .where(
                    Meme.scope_id == self.scope.scope_id,
                    Meme.id == operation.meme_id,
                )
                .with_for_update()
            )
            if record is None:
                if operation.meme_id is not None:
                    try:
                        thumbnail_keys = list(
                            dict.fromkeys(
                                [
                                    *thumbnail_keys,
                                    *self._thumbnail_file_keys(operation.meme_id, str(operation.before_sha256)),
                                ]
                            )
                        )
                    except DatabaseError as exc:
                        if exc.code != "thumbnail_storage_unavailable":
                            raise
                        self._mark_delete_scan_pending(operation, thumbnail_keys, counts)
                        return
                    operation.thumbnail_keys = thumbnail_keys
                operation.meme_id = None
            elif (
                record.storage_key != operation.source_key
                or record.sha256.lower() != str(operation.before_sha256).lower()
                or record.size_bytes != operation.before_size
            ):
                raise DatabaseError("delete_target_changed")
            else:
                thumbnail_rows = self._thumbnail_rows(session, record.id)
                try:
                    thumbnail_keys = list(dict.fromkeys([*thumbnail_keys, *self._thumbnail_keys_for_record(session, record, rows=thumbnail_rows)]))
                except DatabaseError as exc:
                    if exc.code != "thumbnail_storage_unavailable":
                        raise
                    self._mark_delete_scan_pending(operation, thumbnail_keys, counts)
                    return
                operation.thumbnail_keys = thumbnail_keys
                for row in thumbnail_rows:
                    row.status = "stale"
                    row.diagnostic = {"error": "meme_delete_in_progress"}
                    row.updated_at = utcnow()
                operation.meme_id = None
                session.delete(record)
                session.flush()

        original_pending = False
        if quarantine_ok:
            try:
                self.blob_store.unlink(operation.target_key)
            except DatabaseError:
                original_pending = True
        elif operation.status == "file_applied" and operation.meme_id is None and not self._path_present(self.blob_store, operation.target_key):
            # file_applied + Meme 已解除关联时，隔离文件缺失只可能是上一次清理
            # 已成功完成；这使进程在最后一步崩溃后可以安全重试派生清理。
            original_pending = False
        else:
            self._set_status(operation, "blocked", error={"error": "delete_blob_missing", "message": "源文件和隔离文件均不存在"}, session=session)
            counts["blocked"] += 1
            return

        remaining = self._cleanup_thumbnail_files(thumbnail_keys)
        if delete_identity is not None:
            # 第一次清理期间若有已启动的旧 Worker 落位，删除操作没有父行锁可再等它；
            # 完成 operation 前再扫一次同一 marker，避免把晚到孤立文件当成已清理。
            try:
                late_thumbnail_keys = self._thumbnail_file_keys(delete_identity[0], delete_identity[1])
            except DatabaseError as exc:
                if exc.code != "thumbnail_storage_unavailable":
                    raise
                self._mark_delete_scan_pending(operation, remaining, counts)
                return
            if late_thumbnail_keys:
                remaining = self._cleanup_thumbnail_files(list(dict.fromkeys([*remaining, *late_thumbnail_keys])))
        if original_pending or remaining:
            operation.thumbnail_keys = remaining
            operation.error = self._merge_delete_error(
                self._delete_identity_marker(*delete_identity) if delete_identity is not None else None,
                error="storage_cleanup_pending",
                original_pending=original_pending,
                thumbnail_count=len(remaining),
            )
            operation.updated_at = utcnow()
            session.flush()
            counts["retried"] += 1
            return
        operation.thumbnail_keys = []
        self._set_status(operation, "completed", session=session)
        counts["completed"] += 1

    def _scan_snapshot(self) -> tuple[list[Meme], list[StorageOperation]]:
        """读取当前 scope 的扫描快照并归还连接，供事务外目录与文件检查使用。"""
        with self.resources.factory() as session:
            records = list(session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id)))
            operations = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE)))))
        return records, operations

    def flat_preflight(self) -> dict[str, Any]:
        """只读检查业务身份、嵌套图片和记录/文件一致性，供 migration 与启动门禁使用。

        返回值按 scope 聚合结构性问题和物理问题。结构性问题会阻断检索重建；原图
        缺失或指纹漂移只作为报告保留，不能在预检阶段改写 Meme 状态。
        """
        report: dict[str, Any] = {
            "non_flat_keys": [],
            "invalid_sha256": [],
            "invalid_extensions": [],
            "invalid_display_names": [],
            "non_content_addressed_keys": [],
            "duplicate_content": [],
            "nested_images": [],
            "orphan_files": [],
            "missing_files": [],
            "mismatched": [],
            "active_operations": [],
        }
        records, operations = self._scan_snapshot()
        referenced: set[str] = set()
        content_records: dict[tuple[str, str], str] = {}
        for record in records:
            storage_key = record.storage_key if isinstance(record.storage_key, str) else ""
            if storage_key:
                referenced.add(storage_key)
            record_id = str(record.id)
            sha256 = record.sha256 if isinstance(record.sha256, str) else ""
            extension = record.extension if isinstance(record.extension, str) else ""
            sha_valid = re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
            extension_valid = extension in SUPPORTED_EXTENSIONS and extension == extension.lower()
            if not sha_valid:
                report["invalid_sha256"].append(record_id)
            if not extension_valid:
                report["invalid_extensions"].append(record_id)
            display_name = getattr(record, "display_name", None)
            try:
                display_name_valid = isinstance(display_name, str) and normalize_display_name(display_name) == display_name
            except ValueError:
                display_name_valid = False
            if not display_name_valid:
                report["invalid_display_names"].append(record_id)
            try:
                storage_key_valid = validate_business_storage_key(storage_key) == storage_key
            except ValueError:
                storage_key_valid = False
                report["non_flat_keys"].append(storage_key or record_id)
            if not (sha_valid and extension_valid and storage_key == f"{sha256}{extension}"):
                report["non_content_addressed_keys"].append(record_id)
            if sha_valid and extension_valid:
                content_identity = (sha256, extension)
                previous_id = content_records.get(content_identity)
                if previous_id is None:
                    content_records[content_identity] = record_id
                else:
                    report["duplicate_content"].append({"sha256": sha256, "extension": extension, "meme_ids": [previous_id, record_id]})
            # 无效身份只进入结构报告，不交给文件解析器。
            if not storage_key_valid or not sha_valid or not extension_valid:
                continue
            if not self.blob_store.exists_with_identity(storage_key):
                report["missing_files"].append(record_id)
            elif not self.blob_store.exists_with_identity(storage_key, sha256=sha256, size_bytes=record.size_bytes):
                report["mismatched"].append(record_id)
        for path in self.blob_store.root.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                continue
            key = path.relative_to(self.blob_store.root).as_posix()
            if "/" in key and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                report["nested_images"].append(key)
            elif key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                report["orphan_files"].append(key)
        for operation in operations:
            fields = []
            if operation.operation_type == "upload":
                fields = [operation.target_key]
            elif operation.operation_type == "rename":
                fields = [operation.source_key, operation.target_key]
            elif operation.operation_type == "delete":
                fields = [operation.source_key]
            for value in fields:
                if value:
                    try:
                        validate_business_storage_key(value)
                    except ValueError:
                        report["non_flat_keys"].append(value)
        report["active_operations"] = [str(item.id) for item in operations]
        return report

    def integrity_scan(self) -> dict[str, Any]:
        """在事务外检查文件，逐项取得身份锁并复核快照后标记待修复记录。"""
        report: dict[str, Any] = {"orphan_files": [], "missing_files": [], "mismatched": [], "path_conflicts": [], "active_operations": []}
        records, operations = self._scan_snapshot()
        referenced: set[str] = set()
        duplicate_keys: dict[str, list[str]] = {}
        for record in records:
            referenced.add(record.storage_key)
            duplicate_keys.setdefault(record.storage_key, []).append(str(record.id))
            with self.blob_store.identity_lock(record.sha256, record.extension):
                problem = None
                if not self.blob_store.exists_with_identity(record.storage_key):
                    problem = "missing_files"
                elif not self.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes):
                    problem = "mismatched"
                if problem is None:
                    continue
                with self._transaction() as session:
                    current = session.get(Meme, record.id, with_for_update=True)
                    if current is None or current.scope_id != self.scope.scope_id or (current.storage_key, current.sha256, current.size_bytes, current.extension, current.updated_at) != (record.storage_key, record.sha256, record.size_bytes, record.extension, record.updated_at):
                        raise DatabaseError("storage_scan_target_changed")
                    current.context_status = "repair_required"
                    current.updated_at = utcnow()
                report[problem].append(str(record.id))
        for path in self.blob_store.root.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                continue
            key = path.relative_to(self.blob_store.root).as_posix()
            if key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                report["orphan_files"].append(key)
        report["path_conflicts"] = [ids for ids in duplicate_keys.values() if len(ids) > 1]
        report["active_operations"] = [str(item.id) for item in operations]
        return report
