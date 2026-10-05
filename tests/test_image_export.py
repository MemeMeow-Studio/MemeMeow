# 检索尺寸输出的真实图片及 PostgreSQL 验证，覆盖格式、动画、权限和派生清理。

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine, delete, select
from starlette.requests import Request

from backend.gif_compression import scan_gif
from backend.config import Settings
from backend.image_export import DerivedImageExportService, ImageExportConfig, render_image_export
from backend.image_library_http import media
from backend.persistence.models import DerivedImageThumbnail, Scope, ScopeContext
from backend.persistence.resources import DatabaseResources
from backend.persistence.storage import StorageCoordinator
from backend.services.metadata import PostgresMetadataService
from backend.services.thumbnails import DerivedThumbnailService, ThumbnailError
from backend.scope import ScopeServiceFactory
from backend.system_http import error


def test_encoding_worker_imports_only_image_dependencies() -> None:
    """实际启动解释器验证编码入口保持独立，不加载 NumPy 和数据库服务。"""
    subprocess.run(
        [sys.executable, "-c", "import sys; import backend.image_export_worker; assert all(name not in sys.modules for name in (\"numpy\", \"backend.services\", \"backend.persistence.models\"))"],
        check=True,
    )


def image_bytes(size: tuple[int, int], image_format: str = "PNG", **options) -> bytes:
    """生成带透明背景或 RGB 颜色的真实图片，供输出解码测试使用。"""
    mode = "RGBA" if image_format == "PNG" else "RGB"
    color = (30, 60, 90, 128) if mode == "RGBA" else (30, 60, 90)
    with Image.new(mode, size, color) as image:
        output = BytesIO()
        image.save(output, format=image_format, **options)
        return output.getvalue()


def animation_bytes(loop: int | None) -> bytes:
    """生成具有透明区域和不同帧时长的 GIF，供动画缩放测试使用。"""
    frames = []
    for color in (1, 2, 3):
        frame = Image.new("P", (960, 480), color)
        frame.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * 756)
        frame.paste(0, (0, 0, 960, 100))
        frame.info["transparency"] = 0
        frames.append(frame)
    output = BytesIO()
    options = {} if loop is None else {"loop": loop}
    frames[0].save(
        output, format="GIF", save_all=True, append_images=frames[1:],
        duration=[40, 80, 120], transparency=0, disposal=2, optimize=False, **options,
    )
    for frame in frames:
        frame.close()
    return output.getvalue()


@pytest.mark.parametrize("max_edge", [240, 360, 480, 640])
@pytest.mark.parametrize("image_format,extension", [("PNG", ".png"), ("JPEG", ".jpeg")])
def test_static_resize_keeps_ratio_and_format(max_edge: int, image_format: str, extension: str) -> None:
    """解码真实输出验证全部档位保持比例、格式和 PNG 透明通道。"""
    config = ImageExportConfig(profile=f"export-v1-{max_edge}", max_edge=max_edge)
    rendered = render_image_export(image_bytes((960, 480), image_format), extension, config)
    with Image.open(BytesIO(rendered.content)) as result:
        result.load()
        assert result.format == image_format
        assert result.size == (max_edge, max_edge // 2) == (rendered.width, rendered.height)
        if image_format == "PNG":
            assert result.getpixel((10, 10))[3] == 128


def test_small_image_keeps_original_bytes() -> None:
    """尺寸小于档位时保留原始字节，图片不放大。"""
    content = image_bytes((100, 50))
    rendered = render_image_export(content, ".png", ImageExportConfig())
    assert rendered.content == content
    assert (rendered.width, rendered.height) == (100, 50)


def test_small_image_obeys_output_limit() -> None:
    """小图保持尺寸时仍检查输出预算，禁止绕过文件大小限制。"""
    content = image_bytes((100, 50))
    with pytest.raises(ThumbnailError, match="image_export_output_limit_exceeded"):
        render_image_export(content, ".png", ImageExportConfig(max_output_bytes=len(content) - 1))


def test_jpeg_orientation_is_applied() -> None:
    """带 EXIF 方向的 JPEG 输出使用实际显示方向。"""
    orientation = Image.Exif()
    orientation[274] = 6
    rendered = render_image_export(image_bytes((960, 480), "JPEG", exif=orientation), ".jpg", ImageExportConfig())
    with Image.open(BytesIO(rendered.content)) as result:
        assert result.size == (240, 480)
        assert result.getexif().get(274) in {None, 1}


@pytest.mark.parametrize("loop", [None, 0, 3])
def test_gif_resize_keeps_animation(loop: int | None) -> None:
    """使用实际 GIF 编码器验证帧数、帧时长、循环和透明区域。"""
    content = animation_bytes(loop)
    rendered = render_image_export(content, ".gif", ImageExportConfig())
    with Image.open(BytesIO(content)) as source, Image.open(BytesIO(rendered.content)) as result:
        original = scan_gif(source)
        final = scan_gif(result)
        assert final.size == (480, 240)
        assert final.durations == original.durations
        assert final.loop == original.loop
        assert final.transparent_frames == original.transparent_frames == (True, True, True)


@pytest.mark.parametrize("max_edge", [0, 241, 1024, True, "480"])
def test_unlisted_sizes_are_rejected(max_edge) -> None:
    """配置入口拒绝固定档位以外的值和错误类型。"""
    with pytest.raises(ValueError, match="image_export_size_invalid"):
        ImageExportConfig(profile=f"export-v1-{max_edge}", max_edge=max_edge)


def test_resource_limits_and_format_errors_are_reported() -> None:
    """真实解码与编码触发资源上限及格式错误，保留错误码。"""
    content = image_bytes((960, 480))
    with pytest.raises(ThumbnailError, match="image_export_temp_limit_exceeded"):
        render_image_export(content, ".png", ImageExportConfig(max_temp_bytes=1))
    with pytest.raises(ThumbnailError, match="image_export_output_limit_exceeded"):
        render_image_export(content, ".png", ImageExportConfig(max_output_bytes=1))
    with pytest.raises(ThumbnailError, match="invalid_image"):
        render_image_export(content, ".jpg", ImageExportConfig())


def test_export_cache_scope_and_delete(tmp_path: Path) -> None:
    """真实 PostgreSQL 与文件验证缓存复用、scope 隔离、HTTP 条件请求及删除。"""
    database_url = os.environ.get("MEMEMEOW_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式提供开发测试 PostgreSQL 连接")
    engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=3)
    scope = ScopeContext(f"image-export-validation-{uuid4().hex}")
    other_scope = ScopeContext(f"image-export-validation-{uuid4().hex}")
    resources = DatabaseResources(engine, image_root=tmp_path / "images", data_root=tmp_path, require_local_scope=False)
    service_factory = ScopeServiceFactory(resources, Settings(_env_file=None, thumbnail_concurrency=1))
    with resources.factory() as session, session.begin():
        session.add_all([Scope(id=scope.scope_id), Scope(id=other_scope.scope_id)])
    try:
        first_services = service_factory.for_scope(scope)
        second_services = service_factory.for_scope(scope)
        other_services = service_factory.for_scope(other_scope)
        slots = first_services.thumbnails.generation_slots
        assert slots is second_services.thumbnails.generation_slots
        assert slots is other_services.thumbnails.generation_slots
        assert slots.acquire(blocking=False)
        try:
            assert not second_services.thumbnails.generation_slots.acquire(blocking=False)
            assert not other_services.thumbnails.generation_slots.acquire(blocking=False)
        finally:
            slots.release()
        storage = StorageCoordinator(resources, scope_id=scope)
        record = storage.upload(image_bytes((960, 480)), extension=".png", context={}, provenance={})
        thumbnails = DerivedThumbnailService(resources, scope_id=scope)
        services = SimpleNamespace(metadata=PostgresMetadataService(resources, scope_id=scope), thumbnails=thumbnails)

        def provide_services(_request: Request):
            """将同一 scope 的真实服务组合提供给媒体边界。"""
            return services

        def request(etag: str | None = None) -> Request:
            """构造实际 ASGI 媒体请求，按需携带条件缓存标签。"""
            headers = [] if etag is None else [(b"if-none-match", etag.encode("ascii"))]
            return Request({"type": "http", "method": "GET", "path": f"/media/{record.id}", "query_string": b"max_edge=480", "headers": headers})

        source_path = resources.blob_store_for_scope(scope).resolve(record.storage_key, must_exist=True)
        source_permissions = source_path.stat().st_mode
        try:
            source_path.chmod(0)
            with pytest.raises(HTTPException) as unreadable:
                asyncio.run(media(request(), meme_id=str(record.id), max_edge=480, services=provide_services, error=error))
            assert unreadable.value.status_code == 409
            assert unreadable.value.detail["error"] == "thumbnail_source_unavailable"
            assert "读取权限" in unreadable.value.detail["message"]
        finally:
            source_path.chmod(source_permissions)

        paths = []
        for max_edge in (240, 360, 480, 640):
            exports = DerivedImageExportService(thumbnails, max_edge)
            assert exports.generation_slots is thumbnails.generation_slots
            path, media_type, digest = exports.prepare_media(record.id)
            paths.append(path)
            identity = (path.stat().st_mtime_ns, path.read_bytes())
            assert exports.prepare_media(record.id) == (path, media_type, digest)
            assert (path.stat().st_mtime_ns, path.read_bytes()) == identity
            assert media_type == "image/png"
            with Image.open(path) as result:
                assert result.size == (max_edge, max_edge // 2)
        first = asyncio.run(media(request(), meme_id=str(record.id), max_edge=480, services=provide_services, error=error))
        assert first.status_code == 200
        cached = asyncio.run(media(request(first.headers["etag"]), meme_id=str(record.id), max_edge=480, services=provide_services, error=error))
        assert cached.status_code == 304
        assert cached.headers["cache-control"] == "private, no-cache"
        assert cached.headers["vary"] == "Cookie"
        with pytest.raises(HTTPException) as invalid:
            asyncio.run(media(request(), meme_id=str(record.id), max_edge=321, services=provide_services, error=error))
        assert invalid.value.status_code == 422
        assert invalid.value.detail["error"] == "image_export_size_invalid"
        foreign = DerivedImageExportService(DerivedThumbnailService(resources, scope_id=other_scope), 480)
        with pytest.raises(ThumbnailError, match="meme_not_found"):
            foreign.prepare_media(record.id)
        paths[2].unlink()
        assert DerivedImageExportService(thumbnails, 480).prepare_media(record.id)[0].is_file()
        for _index in range(thumbnails.config.concurrency):
            assert thumbnails.generation_slots.acquire(blocking=False)
        try:
            with pytest.raises(ThumbnailError, match="thumbnail_backpressure"):
                DerivedImageExportService(thumbnails, 480).generate(record.id)
        finally:
            for _index in range(thumbnails.config.concurrency):
                thumbnails.generation_slots.release()
        storage.delete(record.id)
        assert all(not path.exists() for path in paths)
        with resources.factory() as session:
            assert not session.scalars(select(DerivedImageThumbnail).where(DerivedImageThumbnail.meme_id == record.id)).all()
        for content, extension, media_type in [
            (image_bytes((480, 960), "JPEG"), ".jpg", "image/jpeg"),
            (animation_bytes(3), ".gif", "image/gif"),
        ]:
            formatted = storage.upload(content, extension=extension, context={}, provenance={})
            exports = DerivedImageExportService(thumbnails, 480)
            path, actual_media_type, digest = exports.prepare_media(formatted.id)
            assert actual_media_type == media_type
            assert exports.prepare_media(formatted.id) == (path, actual_media_type, digest)
            storage.delete(formatted.id)
            assert not path.exists()
    finally:
        service_factory.shutdown()
        with resources.factory() as session, session.begin():
            session.execute(delete(Scope).where(Scope.id.in_([scope.scope_id, other_scope.scope_id])))
        engine.dispose()
