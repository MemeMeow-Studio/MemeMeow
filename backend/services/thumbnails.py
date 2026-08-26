"""缩略图派生服务。

该模块位于 scope-bound 数据资源、独立派生存储和后台任务之间。它负责校验原图
版本、在受限子进程中生成首帧静态缩略图、提交可访问输出并投影有限状态；不修改
原图、语境或现有四阶段图片处理 Job。
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import func, select

from backend.image_safety import ImagePreflightError, validate_image_content
from backend.persistence.engine import DatabaseError
from backend.persistence.models import DerivedImageThumbnail, Meme, ScopeContext, Task, utcnow
from backend.persistence.resources import DatabaseResources
from backend.persistence.repositories.thumbnails import THUMBNAIL_STATUSES
from backend.thumbnail_config import (
    THUMBNAIL_OUTPUT_EXTENSION,
    THUMBNAIL_OUTPUT_MEDIA_TYPE,
    ThumbnailConfig,
)


class ThumbnailError(RuntimeError):
    """缩略图派生或访问失败的稳定领域错误。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RenderedThumbnail:
    """受限生成器返回的完整输出事实。"""

    content: bytes
    width: int
    height: int


def _thumbnail_worker(content: bytes, extension: str, max_edge: int, output_limit: int, temp_limit: int, connection: Any) -> None:
    """在独立进程中解码首帧并以小块消息返回 PNG 输出。"""
    try:
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF"}.get(extension.lower())
        if expected is None:
            raise ThumbnailError("unsupported_format")
        with Image.open(BytesIO(content)) as image:
            if image.format != expected:
                raise ThumbnailError("invalid_image")
            if getattr(image, "n_frames", 1) > 1:
                image.seek(0)
            width, height = image.size
            channels = 4 if "A" in image.getbands() or image.mode in {"P", "LA"} else 3
            if width < 1 or height < 1 or width * height * channels > temp_limit:
                raise ThumbnailError("thumbnail_temp_limit_exceeded")
            image.load()
            converted = image.convert("RGBA" if channels == 4 else "RGB")
            converted.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            converted.save(output, format="PNG", optimize=True)
            result = output.getvalue()
            if not result or len(result) > output_limit:
                raise ThumbnailError("thumbnail_output_limit_exceeded")
            result_width, result_height = converted.size
        connection.send_bytes(b"M" + json.dumps({"width": result_width, "height": result_height, "size": len(result)}).encode("ascii"))
        for offset in range(0, len(result), 64 * 1024):
            connection.send_bytes(b"D" + result[offset : offset + 64 * 1024])
        connection.send_bytes(b"E")
    except ThumbnailError as exc:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send_bytes(b"X" + exc.code.encode("ascii", errors="replace"))
    except Exception:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send_bytes(b"Xinvalid_image")
    finally:
        with suppress(OSError):
            connection.close()


def _process_context() -> Any:
    """选择适合测试和服务进程的图片派生子进程上下文。"""
    main_file = getattr(__import__("__main__"), "__file__", None)
    if isinstance(main_file, str) and Path(main_file).is_file():
        return multiprocessing.get_context("spawn")
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("fork" if "fork" in methods else "spawn")


def _render_thumbnail(content: bytes, extension: str, config: ThumbnailConfig) -> RenderedThumbnail:
    """以超时和消息大小边界运行图片缩略图生成子进程。"""
    context = _process_context()
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_thumbnail_worker,
        args=(content, extension, config.max_edge, config.max_output_bytes, config.max_temp_bytes, child),
        daemon=True,
    )
    try:
        process.start()
    except (AssertionError, OSError, RuntimeError) as exc:
        child.close()
        parent.close()
        raise ThumbnailError("thumbnail_generation_unavailable") from exc
    child.close()
    deadline = time.monotonic() + float(config.timeout_seconds)
    metadata: dict[str, int] | None = None
    chunks: list[bytes] = []
    try:
        finished = False
        while time.monotonic() < deadline:
            if parent.poll(min(0.05, max(0.0, deadline - time.monotonic()))):
                message = parent.recv_bytes()
                if not message:
                    raise ThumbnailError("thumbnail_generation_failed")
                marker, payload = message[:1], message[1:]
                if marker == b"X":
                    raise ThumbnailError(payload.decode("ascii", errors="replace") or "thumbnail_generation_failed")
                if marker == b"M":
                    try:
                        parsed = json.loads(payload.decode("ascii"))
                        raw_width = parsed["width"]
                        raw_height = parsed["height"]
                        raw_size = parsed["size"]
                        if any(isinstance(value, bool) for value in (raw_width, raw_height, raw_size)):
                            raise ValueError("boolean metadata")
                        metadata = {"width": int(raw_width), "height": int(raw_height), "size": int(raw_size)}
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ThumbnailError("thumbnail_generation_failed") from exc
                    if metadata["width"] < 1 or metadata["height"] < 1 or max(metadata["width"], metadata["height"]) > config.max_edge:
                        raise ThumbnailError("thumbnail_output_invalid")
                    if metadata["size"] < 1 or metadata["size"] > config.max_output_bytes:
                        raise ThumbnailError("thumbnail_output_limit_exceeded")
                elif marker == b"D":
                    if metadata is None:
                        raise ThumbnailError("thumbnail_generation_failed")
                    chunks.append(payload)
                    if sum(map(len, chunks)) > config.max_output_bytes:
                        raise ThumbnailError("thumbnail_output_limit_exceeded")
                elif marker == b"E":
                    finished = True
                    break
            elif not process.is_alive():
                break
        if not finished:
            if time.monotonic() >= deadline:
                raise ThumbnailError("thumbnail_generation_timeout")
            raise ThumbnailError("thumbnail_generation_failed")
        if metadata is None:
            raise ThumbnailError("thumbnail_generation_failed")
        output = b"".join(chunks)
        if len(output) != metadata["size"]:
            raise ThumbnailError("thumbnail_generation_failed")
        if max(metadata["width"], metadata["height"]) > config.max_edge:
            raise ThumbnailError("thumbnail_output_invalid")
        return RenderedThumbnail(output, metadata["width"], metadata["height"])
    except (EOFError, OSError) as exc:
        raise ThumbnailError("thumbnail_generation_failed") from exc
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)
        if process.is_alive() and callable(getattr(process, "kill", None)):
            process.kill()
            process.join(timeout=1.0)
        parent.close()


class DerivedThumbnailService:
    """当前 scope 的缩略图生成、投影、回填和清理服务。"""

    TASK_TYPE = "derived_thumbnail_generation"

    def __init__(
        self,
        resources: DatabaseResources,
        settings: Any | None = None,
        *,
        scope_id: str | ScopeContext = "local",
        config: ThumbnailConfig | None = None,
        task_service: Any | None = None,
    ):
        """绑定数据库、独立派生存储、配置和可选的 durable task service。"""
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.config = config or (ThumbnailConfig.from_settings(settings) if settings is not None else ThumbnailConfig())
        self.store = resources.thumbnail_store_for_scope(self.scope)
        self.task_service = task_service
        self._slots = BoundedSemaphore(self.config.concurrency)

    @staticmethod
    def _output_key(meme: Meme, profile: str) -> str:
        """为源版本计算内部唯一输出 key，不将其暴露给客户端。"""
        return f"{meme.id.hex}-{str(meme.sha256).lower()}-{profile}{THUMBNAIL_OUTPUT_EXTENSION}"

    @staticmethod
    def _task_dedupe_key(payload: dict[str, object]) -> str:
        """复用 PostgreSQL 任务服务对普通任务使用的稳定去重键规则。"""
        return f"{DerivedThumbnailService.TASK_TYPE}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    @staticmethod
    def _identity(path: Path) -> tuple[int, str]:
        """读取文件大小和 SHA-256，用于源版本和输出完整性复核。"""
        digest = hashlib.sha256()
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ThumbnailError("thumbnail_source_unavailable") from exc
        return size, digest.hexdigest()

    @staticmethod
    def _read_identity(path: Path) -> tuple[int, str, bytes]:
        """单次读取原图并同时返回字节、大小和 SHA-256，供生成流程复用。"""
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    chunks.append(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise ThumbnailError("thumbnail_source_unavailable") from exc
        content = b"".join(chunks)
        return len(content), digest.hexdigest(), content

    def _meme(self, meme_id: UUID | str, *, for_update: bool = False) -> Meme:
        """按当前 scope 读取 Meme；生成、媒体和投影都只接受稳定 ID。"""
        with self.resources.environment(self.scope) as environment:
            meme = environment.memes.get(meme_id, for_update=for_update)
        if meme is None:
            raise ThumbnailError("meme_not_found")
        return meme

    def _mark_source_stale(self, meme: Meme, error: str) -> None:
        """在原图不可验证时阻断旧派生访问，并保留可恢复诊断。"""
        with self.resources.environment(self.scope) as environment:
            environment.thumbnails.mark_stale(meme.id, self.config.profile, diagnostic={"error": error})

    def _meme_and_source(self, meme_id: UUID | str) -> tuple[Meme, Path, bytes]:
        """按当前 scope 解析并严格校验 Meme 原图，且只读取原图一次。"""
        meme = self._meme(meme_id)
        try:
            source_store = self.resources.blob_store_for_scope(self.scope)
            path = source_store.resolve(meme.storage_key, must_exist=True)
            size, digest, content = self._read_identity(path)
        except (DatabaseError, ThumbnailError) as exc:
            self._mark_source_stale(meme, "source_unavailable")
            raise ThumbnailError("thumbnail_source_unavailable") from exc
        if size != meme.size_bytes or digest.lower() != str(meme.sha256).lower():
            self._mark_source_stale(meme, "source_version_changed")
            raise ThumbnailError("thumbnail_source_changed")
        return meme, path, content

    def _meme_and_source_identity(self, meme_id: UUID | str) -> tuple[Meme, Path]:
        """只解析并验证原图身份，媒体读取路径不加载原图字节。"""
        meme = self._meme(meme_id)
        try:
            source_store = self.resources.blob_store_for_scope(self.scope)
            path = source_store.resolve(meme.storage_key, must_exist=True)
            size, digest = self._identity(path)
        except (DatabaseError, ThumbnailError) as exc:
            self._mark_source_stale(meme, "source_unavailable")
            raise ThumbnailError("thumbnail_source_unavailable") from exc
        if size != meme.size_bytes or digest.lower() != str(meme.sha256).lower():
            self._mark_source_stale(meme, "source_version_changed")
            raise ThumbnailError("thumbnail_source_changed")
        return meme, path

    def _source_identity_error(self, meme: Meme, source_identity: tuple[int, str] | None = None) -> str | None:
        """只读取当前原图身份，返回不可公开的失效原因或 ``None``。

        投影和媒体解析不能信任数据库保存的旧 SHA/大小；该轻量校验不读取完整
        内容，生成流程仍由 ``_meme_and_source`` 负责取得字节。列表调用可传入已经
        验证过的同一请求身份，避免为同一原图重复计算 SHA。
        """
        if source_identity is None:
            try:
                source_store = self.resources.blob_store_for_scope(self.scope)
                path = source_store.resolve(meme.storage_key, must_exist=True)
                size, digest = self._identity(path)
            except (DatabaseError, ThumbnailError):
                return "source_unavailable"
        else:
            size, digest = source_identity
            if type(size) is not int or not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                return "source_unavailable"
        if size != meme.size_bytes or digest.lower() != str(meme.sha256).lower():
            return "source_version_changed"
        return None

    def _output_is_valid(self, meme: Meme, row: DerivedImageThumbnail) -> bool:
        """严格复核可访问输出的绑定、元数据和物理文件指纹。"""
        output_sha = row.output_sha256
        output_size = row.output_size_bytes
        width = row.width
        height = row.height
        if row.output_key != self._output_key(meme, self.config.profile):
            return False
        if not isinstance(output_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", output_sha) is None:
            return False
        if type(output_size) is not int or output_size < 1:
            return False
        if type(width) is not int or type(height) is not int or width < 1 or height < 1:
            return False
        if max(width, height) > self.config.max_edge:
            return False
        if row.media_type != THUMBNAIL_OUTPUT_MEDIA_TYPE:
            return False
        return self.store.exists_with_identity(row.output_key, sha256=output_sha.lower(), size_bytes=output_size)

    def _thumbnail_file_keys(self, meme: Meme) -> list[str]:
        """收集指定 Meme 同源但尚未落库的派生输出，供删除收尾使用。"""
        prefix = f"{meme.id.hex}-{str(meme.sha256).lower()}-"
        try:
            return [
                path.name
                for path in self.store.root.iterdir()
                if path.is_file() and not path.is_symlink() and path.name.startswith(prefix)
            ]
        except OSError as exc:
            # 未完成扫描时无法证明没有孤立输出；清理调用方应保留事实并让恢复任务
            # 在派生存储恢复后重试，而不是提交一个不完整的清理结果。
            raise ThumbnailError("thumbnail_cleanup_failed") from exc

    @staticmethod
    def _mark_row_stale(row: DerivedImageThumbnail, error: str) -> None:
        """将当前派生事实置为不可访问的 stale，并保留输出 key 供清理器回收。"""
        row.status = "stale"
        row.diagnostic = {"error": error}
        row.updated_at = utcnow()

    @staticmethod
    def _projection(row: DerivedImageThumbnail | None, *, meme_id: UUID | str) -> dict[str, object]:
        """把数据库状态转换为不暴露内部 key 的 API 缩略图对象。"""
        status = row.status if row is not None and row.status in THUMBNAIL_STATUSES else "pending"
        payload: dict[str, object] = {"status": status, "media_url": None}
        if status == "available" and row is not None and row.output_key:
            payload["media_url"] = f"/media/{meme_id}/thumbnail"
            if row.width is not None:
                payload["width"] = row.width
            if row.height is not None:
                payload["height"] = row.height
            payload["media_type"] = row.media_type or THUMBNAIL_OUTPUT_MEDIA_TYPE
        return payload

    def projection(self, meme: Meme, *, ensure: bool = True, source_identity: tuple[int, str] | None = None) -> dict[str, object]:
        """返回当前 Meme 的有限缩略图状态，必要时持久化 pending。"""
        with self.resources.environment(self.scope) as environment:
            row = environment.thumbnails.current(meme, self.config.profile)
            source_error = self._source_identity_error(meme, source_identity)
            if row is None and ensure and source_error is None:
                try:
                    row = environment.thumbnails.ensure_pending(meme, self.config.profile)
                except DatabaseError as exc:
                    # 列表或合集拿到已删除 Meme 的快照时，FK 竞态只应使该投影
                    # 不可用，不应把读请求升级为 500。
                    if exc.code == "meme_not_found":
                        return {"status": "stale", "media_url": None}
                    if exc.code == "thumbnail_pending_conflict":
                        return {"status": "pending", "media_url": None}
                    raise
            if source_error is not None:
                if row is not None:
                    self._mark_row_stale(row, source_error)
                    environment.uow.session.flush()
                    return self._projection(row, meme_id=meme.id)
                return {"status": "stale", "media_url": None}
            if row is not None and row.status == "available" and not self._output_is_valid(meme, row):
                self._mark_row_stale(row, "thumbnail_output_unavailable")
                environment.uow.session.flush()
            return self._projection(row, meme_id=meme.id)

    def projection_for_meme_id(self, meme_id: UUID | str) -> dict[str, object]:
        """按稳定 Meme ID 读取当前 scope 的缩略图状态。"""
        with self.resources.environment(self.scope) as environment:
            meme = environment.memes.get(meme_id)
        if meme is None:
            return {"status": "stale", "media_url": None}
        return self.projection(meme)

    def projections(
        self,
        memes: list[Meme],
        *,
        source_identities: Mapping[UUID, tuple[int, str]] | None = None,
    ) -> dict[UUID, dict[str, object]]:
        """按一页 Meme 批量投影并为缺失事实建立 pending 状态。"""
        result: dict[UUID, dict[str, object]] = {}
        with self.resources.environment(self.scope) as environment:
            rows = environment.thumbnails.list_current(memes, self.config.profile)
            for meme in memes:
                row = rows.get(meme.id)
                source_identity = source_identities.get(meme.id) if source_identities is not None else None
                source_error = self._source_identity_error(meme, source_identity)
                if row is None and source_error is None:
                    try:
                        row = environment.thumbnails.ensure_pending(meme, self.config.profile)
                    except DatabaseError as exc:
                        if exc.code == "meme_not_found":
                            result[meme.id] = {"status": "stale", "media_url": None}
                            continue
                        if exc.code == "thumbnail_pending_conflict":
                            result[meme.id] = {"status": "pending", "media_url": None}
                            continue
                        raise
                if source_error is not None:
                    if row is not None:
                        self._mark_row_stale(row, source_error)
                elif row is not None and row.status == "available" and not self._output_is_valid(meme, row):
                    self._mark_row_stale(row, "thumbnail_output_unavailable")
                result[meme.id] = self._projection(row, meme_id=meme.id)
            environment.uow.session.flush()
        return result

    def _mark_failure(self, meme: Meme | None, code: str, *, elapsed_ms: int | None = None) -> None:
        """仅将当前源版本标记为 failed，并保存可诊断但不敏感的原因。"""
        if meme is None:
            return
        with self.resources.environment(self.scope) as environment:
            current = environment.memes.get(meme.id, for_update=True)
            if current is None or current.sha256.lower() != str(meme.sha256).lower() or current.size_bytes != meme.size_bytes:
                return
            existing = environment.thumbnails.current(current, self.config.profile, for_update=True)
            # 另一 Worker 可能已经以同一确定输出完成；失败 Worker 不能把成功事实
            # 回写成 failed。输出身份通过同一严格校验，损坏的 available 仍可重建。
            if existing is not None and existing.status == "available" and self._output_is_valid(current, existing):
                return
            diagnostic: dict[str, object] = {"error": code, "updated_at": utcnow().isoformat()}
            if elapsed_ms is not None:
                diagnostic["elapsed_ms"] = elapsed_ms
            environment.thumbnails.mark_failed(current, self.config.profile, diagnostic)

    def generate(
        self,
        meme_id: UUID | str,
        *,
        source_sha256: str | None = None,
        source_size_bytes: int | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        """幂等生成一张当前 scope Meme 的缩略图并返回状态投影。"""
        if profile is not None and profile != self.config.profile:
            raise ThumbnailError("thumbnail_profile_mismatch")
        meme: Meme | None = None
        started = time.monotonic()
        if not self._slots.acquire(blocking=False):
            raise ThumbnailError("thumbnail_backpressure")
        staged_key: str | None = None
        installed_output_key: str | None = None
        installed_output_sha: str | None = None
        installed_output_size: int | None = None
        try:
            meme, _path, content = self._meme_and_source(meme_id)
            if source_sha256 is not None and source_sha256.lower() != str(meme.sha256).lower():
                raise ThumbnailError("thumbnail_source_changed")
            if source_size_bytes is not None and source_size_bytes != meme.size_bytes:
                raise ThumbnailError("thumbnail_source_changed")
            with self.resources.environment(self.scope) as environment:
                row = environment.thumbnails.current(meme, self.config.profile, for_update=True)
                if row is not None and row.status == "available" and self._output_is_valid(meme, row):
                    return self._projection(row, meme_id=meme.id)
                if row is not None and row.status == "available":
                    self._mark_row_stale(row, "thumbnail_output_unavailable")
                    environment.uow.session.flush()
                environment.thumbnails.mark_pending(meme, self.config.profile)
            try:
                validate_image_content(content, meme.extension, timeout_seconds=self.config.timeout_seconds)
                rendered = _render_thumbnail(content, meme.extension, self.config)
            except ImagePreflightError as exc:
                raise ThumbnailError(exc.code) from exc
            # 生成可能跨越原图重命名；再次以 SHA/大小复核即可保留同一派生身份。
            current_meme, _current_path = self._meme_and_source_identity(meme.id)
            if current_meme.sha256.lower() != str(meme.sha256).lower() or current_meme.size_bytes != meme.size_bytes:
                raise ThumbnailError("thumbnail_source_changed")
            if type(rendered.width) is not int or type(rendered.height) is not int or rendered.width < 1 or rendered.height < 1 or max(rendered.width, rendered.height) > self.config.max_edge:
                raise ThumbnailError("thumbnail_output_invalid")
            if not isinstance(rendered.content, bytes) or not 1 <= len(rendered.content) <= self.config.max_output_bytes:
                raise ThumbnailError("thumbnail_output_invalid")
            output_key = self._output_key(current_meme, self.config.profile)
            output_sha = hashlib.sha256(rendered.content).hexdigest()
            with self.resources.environment(self.scope) as environment:
                # 生成 Worker 在安装文件前锁住父 Meme，并一直持锁到派生事实提交；
                # 删除事务因此能在最终快照中看到本次输出，避免留下 orphan 文件。
                final_meme = environment.memes.get(current_meme.id, for_update=True)
                if final_meme is None or final_meme.sha256.lower() != str(meme.sha256).lower() or final_meme.size_bytes != meme.size_bytes:
                    raise ThumbnailError("thumbnail_source_changed")
                if not self.store.exists_with_identity(output_key, sha256=output_sha, size_bytes=len(rendered.content)):
                    token = uuid4()
                    staged_key = self.store.stage_bytes(rendered.content, token=token)
                    if staged_key is not None:
                        try:
                            self.store.link_move(staged_key, output_key)
                        except DatabaseError as exc:
                            if exc.code != "target_exists":
                                raise
                            # link_move 的目标检查和链接之间仍存在窗口；同一
                            # 指纹已经原子落位时，后到者直接复用成功输出。
                            if self.store.exists_with_identity(output_key, sha256=output_sha, size_bytes=len(rendered.content)):
                                self.store.unlink(staged_key)
                                staged_key = None
                            else:
                                # 目标是旧损坏文件，允许本次重建替换；若另一个 Worker
                                # 同时修复，下一次 target_exists 会重新按指纹判定。
                                self.store.unlink(output_key)
                                self.store.link_move(staged_key, output_key)
                                staged_key = None
                                installed_output_key = output_key
                                installed_output_sha = output_sha
                                installed_output_size = len(rendered.content)
                        else:
                            staged_key = None
                            installed_output_key = output_key
                            installed_output_sha = output_sha
                            installed_output_size = len(rendered.content)
                environment.thumbnails.mark_available(
                    final_meme,
                    self.config.profile,
                    output_key=output_key,
                    output_sha256=output_sha,
                    output_size_bytes=len(rendered.content),
                    width=rendered.width,
                    height=rendered.height,
                    media_type=THUMBNAIL_OUTPUT_MEDIA_TYPE,
                )
                row = environment.thumbnails.current(final_meme, self.config.profile)
                return self._projection(row, meme_id=final_meme.id)
        except (ThumbnailError, DatabaseError) as caught:
            exc = caught if isinstance(caught, ThumbnailError) else ThumbnailError(caught.code)
            if staged_key is not None:
                with suppress(DatabaseError):
                    if self.store.exists_with_identity(staged_key):
                        self.store.unlink(staged_key)
            if installed_output_key is not None:
                with suppress(DatabaseError):
                    keep_output = False
                    with self.resources.environment(self.scope) as environment:
                        current = environment.memes.get(meme.id) if meme is not None else None
                        if current is not None:
                            row = environment.thumbnails.current(current, self.config.profile)
                            keep_output = (
                                row is not None
                                and row.status == "available"
                                and row.output_key == installed_output_key
                                and row.output_sha256 == installed_output_sha
                                and row.output_size_bytes == installed_output_size
                            )
                    if not keep_output and self.store.exists_with_identity(installed_output_key, sha256=installed_output_sha, size_bytes=installed_output_size):
                        self.store.unlink(installed_output_key)
            if exc.code == "thumbnail_source_changed" and meme is not None:
                with self.resources.environment(self.scope) as environment:
                    environment.thumbnails.mark_stale(meme.id, self.config.profile, diagnostic={"error": exc.code})
            elif exc.code not in {"meme_not_found", "thumbnail_source_unavailable"}:
                self._mark_failure(meme, exc.code, elapsed_ms=int((time.monotonic() - started) * 1000))
            if isinstance(caught, DatabaseError):
                raise exc from caught
            raise
        finally:
            self._slots.release()

    def enqueue(self, meme_id: UUID | str, *, reset_failed: bool = False) -> Any:
        """建立或复用当前源版本的 durable 缩略图任务，不触碰用户配额。"""
        if self.task_service is None or not any(callable(getattr(self.task_service, name, None)) for name in ("submit_thumbnail", "submit")):
            raise ThumbnailError("thumbnail_task_unavailable")
        with self.resources.environment(self.scope) as environment:
            meme = environment.memes.get(meme_id)
            if meme is None:
                raise ThumbnailError("meme_not_found")
            row = environment.thumbnails.ensure_pending(meme, self.config.profile, reset_failed=reset_failed)
            if row.status == "available" and self._output_is_valid(meme, row):
                return None
            if row.status == "available":
                self._mark_row_stale(row, "thumbnail_output_unavailable")
                environment.uow.session.flush()
                row = environment.thumbnails.mark_pending(meme, self.config.profile)
            payload = {
                "meme_id": str(meme.id),
                "image_sha256": str(meme.sha256).lower(),
                "source_size_bytes": meme.size_bytes,
                "profile": self.config.profile,
            }
            if not callable(getattr(self.task_service, "submit_thumbnail", None)):
                # 轻量兼容 task facade 没有原子 lane 参数，只能在同一
                # scope 事务中执行尽力 admission；正式 PostgreSQL facade
                # 通过 submit_thumbnail 在插入事务内完成同样检查。
                session = getattr(getattr(environment, "uow", None), "session", None)
                if session is not None:
                    dedupe_key = self._task_dedupe_key(payload)
                    existing = session.scalar(
                        select(Task).where(
                            Task.scope_id == self.scope.scope_id,
                            Task.task_type == self.TASK_TYPE,
                            Task.dedupe_key == dedupe_key,
                            Task.status.in_(("queued", "running")),
                        )
                    )
                    if existing is None:
                        active = session.scalar(
                            select(func.count())
                            .select_from(Task)
                            .where(
                                Task.scope_id == self.scope.scope_id,
                                Task.task_type == self.TASK_TYPE,
                                Task.status.in_(("queued", "running")),
                            )
                        ) or 0
                        if int(active) >= self.config.backpressure:
                            raise ThumbnailError("thumbnail_backpressure")
        try:
            submit_thumbnail = getattr(self.task_service, "submit_thumbnail", None)
            if callable(submit_thumbnail):
                return submit_thumbnail(payload, backpressure=self.config.backpressure)
            return self.task_service.submit(self.TASK_TYPE, payload)
        except (DatabaseError, RuntimeError) as exc:
            raise ThumbnailError(str(getattr(exc, "code", None) or str(exc).split(":", 1)[0])) from exc

    def reconcile(self, *, page: int = 1, page_size: int | None = None, limit: int | None = None) -> dict[str, int]:
        """按当前 scope 分页建立存量缩略图任务，重复调用保持幂等。"""
        size = min(max(1, int(page_size or self.config.reconcile_batch_size)), self.config.reconcile_batch_size)
        max_items = min(max(1, int(limit or size)), self.config.reconcile_batch_size)
        with self.resources.environment(self.scope) as environment:
            memes = environment.memes.list(page=max(1, int(page)), page_size=size)
        submitted = 0
        available = 0
        failed = 0
        for meme in memes[:max_items]:
            try:
                row = self.projection(meme)
                if row["status"] == "available":
                    available += 1
                    continue
                if self.enqueue(meme.id) is not None:
                    submitted += 1
            except ThumbnailError as exc:
                if exc.code in {"thumbnail_backpressure", "thumbnail_task_unavailable"}:
                    raise
                failed += 1
        return {"scanned": len(memes[:max_items]), "submitted": submitted, "available": available, "failed": failed}

    def media_path(self, meme_id: UUID | str) -> tuple[Path, str]:
        """按可信 scope 与稳定 Meme ID 返回可访问缩略图路径和媒体类型。"""
        try:
            meme, _path = self._meme_and_source_identity(meme_id)
        except ThumbnailError as exc:
            raise ThumbnailError("thumbnail_not_found") from exc
        with self.resources.environment(self.scope) as environment:
            row = environment.thumbnails.current(meme, self.config.profile)
            if row is None or row.status != "available" or not row.output_key:
                raise ThumbnailError("thumbnail_not_found")
            if not self._output_is_valid(meme, row):
                self._mark_row_stale(row, "thumbnail_output_unavailable")
                environment.uow.session.flush()
                raise ThumbnailError("thumbnail_not_found")
            return self.store.resolve(row.output_key, must_exist=True), row.media_type or THUMBNAIL_OUTPUT_MEDIA_TYPE

    def cleanup_for_meme(self, meme_id: UUID | str) -> int:
        """清理指定 Meme 的派生文件和事实，供删除及离线恢复使用。"""
        try:
            identifier = meme_id if isinstance(meme_id, UUID) else UUID(str(meme_id))
        except (TypeError, ValueError) as exc:
            raise ThumbnailError("meme_not_found") from exc
        keys: list[str] = []
        with self.resources.environment(self.scope) as environment:
            meme = environment.memes.get(identifier)
            rows = list(
                environment.uow.session.scalars(
                    select(DerivedImageThumbnail).where(
                        DerivedImageThumbnail.scope_id == self.scope.scope_id,
                        DerivedImageThumbnail.meme_id == identifier,
                    )
                )
            )
            keys = [row.output_key for row in rows if row.output_key]
            if meme is not None:
                keys.extend(self._thumbnail_file_keys(meme))
            for row in rows:
                self._mark_row_stale(row, "meme_cleanup_in_progress")
            environment.uow.session.flush()
        removed = 0
        for key in dict.fromkeys(keys):
            try:
                path = self.store._key_path(key, must_exist=False)
                if path.exists() or path.is_symlink():
                    self.store.unlink(key)
                    removed += 1
            except DatabaseError as exc:
                raise ThumbnailError("thumbnail_cleanup_failed") from exc
        try:
            with self.resources.environment(self.scope) as environment:
                # 文件清理期间生成 Worker 可能已把新事实提交；锁住父 Meme 后重读
                # 全部 key，覆盖首次收集与最终删除之间的窗口。
                meme = environment.memes.get(identifier, for_update=True)
                final_rows = list(
                    environment.uow.session.scalars(
                        select(DerivedImageThumbnail).where(
                            DerivedImageThumbnail.scope_id == self.scope.scope_id,
                            DerivedImageThumbnail.meme_id == identifier,
                        ).with_for_update()
                    )
                )
                keys.extend(row.output_key for row in final_rows if row.output_key)
                if meme is not None:
                    keys.extend(self._thumbnail_file_keys(meme))
                for row in final_rows:
                    self._mark_row_stale(row, "meme_cleanup_in_progress")
                environment.thumbnails.delete_for_meme(identifier)
                environment.uow.session.flush()
            for key in dict.fromkeys(keys):
                try:
                    path = self.store._key_path(key, must_exist=False)
                    if path.exists() or path.is_symlink():
                        self.store.unlink(key)
                        removed += 1
                except DatabaseError as exc:
                    raise ThumbnailError("thumbnail_cleanup_failed") from exc
        except (DatabaseError, OSError) as exc:
            raise ThumbnailError("thumbnail_cleanup_failed") from exc
        return removed


__all__ = ["DerivedThumbnailService", "RenderedThumbnail", "ThumbnailError", "_render_thumbnail"]
