# 使用真实 PostgreSQL 和媒体文件验证缩略图的单查询读取及线程执行。

import asyncio
import os
import sys
import threading
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, event, select
from starlette.requests import Request

from backend.image_library_http import thumbnail_media
from backend.persistence.engine import create_engine_for_url
from backend.persistence.models import DerivedImageThumbnail, Scope, StorageOperation
from backend.persistence.resources import DatabaseResources
from backend.persistence.storage import StorageCoordinator
from backend.services.thumbnails import DerivedThumbnailService, ThumbnailError


@pytest.fixture
def thumbnail_case(tmp_path):
    """建立独立 scope 和真实缩略图，结束时仅清理本次 scope。"""
    engine = create_engine_for_url(os.environ["MEMEMEOW_TEST_DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=3)
    scopes = [f"thumbnail-query-{uuid4().hex}" for _ in range(2)]
    with engine.begin() as connection:
        connection.execute(Scope.__table__.insert(), [{"id": scope} for scope in scopes])
    resources = DatabaseResources(engine, image_root=tmp_path / "images", data_root=tmp_path / "data", require_local_scope=False)
    try:
        buffer = BytesIO()
        Image.new("RGB", (16, 8), "green").save(buffer, format="PNG")
        record = StorageCoordinator(resources, scope_id=scopes[0]).upload(buffer.getvalue(), extension=".png", context={}, provenance={})
        services = [DerivedThumbnailService(resources, scope_id=scope) for scope in scopes]
        assert services[0].generate(record.id)["status"] == "available"
        yield resources, record, services
    finally:
        with engine.begin() as connection:
            connection.execute(delete(StorageOperation).where(StorageOperation.scope_id.in_(scopes)))
            connection.execute(delete(Scope).where(Scope.id.in_(scopes)))
        engine.dispose()


def test_media_output_single_query_and_connection_release(thumbnail_case):
    """读取真实输出只执行一条 SELECT，路径解析期间连接已经归还。"""
    resources, record, services = thumbnail_case
    reads = []
    file_calls = []
    path_connections = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        """记录实际 SQL，不替换数据库执行。"""
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    def observe_files(frame, event, argument):
        """观察真实路径解析的连接占用及完整图片读取入口。"""
        if event == "call":
            if frame.f_code.co_name in {"_meme_and_source", "_read_identity", "_identity", "image_sha256"}:
                file_calls.append(frame.f_code.co_name)
            if frame.f_code.co_name == "resolve" and frame.f_globals.get("__name__") == "backend.persistence.storage":
                path_connections.append(resources.engine.pool.checkedout())

    event.listen(resources.engine, "before_cursor_execute", observe)
    sys.setprofile(observe_files)
    try:
        path, media_type, digest = services[0].media_path(record.id)
    finally:
        sys.setprofile(None)
        event.remove(resources.engine, "before_cursor_execute", observe)
    assert len(reads) == 1
    assert resources.engine.pool.checkedout() == 0
    assert path_connections == [0] and file_calls == []
    assert path.is_file() and media_type == "image/jpeg" and len(digest) == 64


@pytest.mark.parametrize("field,value", [
    ("source_sha256", "a" * 64),
    ("source_size_bytes", 99999),
    ("profile", "another-profile"),
    ("status", "pending"),
    ("status", "failed"),
    ("status", "stale"),
    ("output_key", None),
    ("output_key", ""),
    ("output_sha256", None),
])
def test_media_output_rejects_unavailable_identity(thumbnail_case, field, value):
    """真实数据库中的版本、规格、状态或输出缺失时拒绝媒体访问。"""
    resources, record, services = thumbnail_case
    with resources.factory() as session, session.begin():
        row = session.scalar(select(DerivedImageThumbnail).where(DerivedImageThumbnail.scope_id == services[0].scope.scope_id, DerivedImageThumbnail.meme_id == record.id))
        setattr(row, field, value)
    with pytest.raises(ThumbnailError, match="thumbnail_not_found"):
        services[0].media_path(record.id)
    assert resources.engine.pool.checkedout() == 0


def test_media_output_scope_and_missing_image(thumbnail_case):
    """其他 scope 与不存在的图片均不能取得已生成的媒体输出。"""
    _, record, services = thumbnail_case
    with pytest.raises(ThumbnailError, match="thumbnail_not_found"):
        services[1].media_path(record.id)
    with pytest.raises(ThumbnailError, match="thumbnail_not_found"):
        services[0].media_path(uuid4())
    with pytest.raises(ThumbnailError, match="thumbnail_not_found"):
        services[0].media_path("invalid-uuid")


def test_thumbnail_http_pool_wait_keeps_event_loop_running(thumbnail_case):
    """实际占用唯一连接时，HTTP 查询在工作线程等待，事件循环仍可运行。"""
    resources, record, services = thumbnail_case
    threads = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        """记录实际媒体查询的线程身份。"""
        threads.append(threading.get_ident())

    def provide_services(request):
        """向媒体 HTTP 入口提供本次 scope 的真实服务。"""
        return SimpleNamespace(thumbnails=services[0])

    def error(status, code, message):
        """使用真实 HTTPException 保留入口的错误映射。"""
        return HTTPException(status, {"error": code, "message": message})

    async def exercise():
        """协调实际连接占用与 HTTP 调用，并检查条件响应。"""
        owner = threading.get_ident()
        holder = resources.engine.connect()
        event.listen(resources.engine, "before_cursor_execute", observe)
        request = Request({"type": "http", "method": "GET", "path": f"/media/{record.id}/thumbnail", "headers": []})
        pending = asyncio.create_task(thumbnail_media(request, meme_id=str(record.id), services=provide_services, error=error))
        try:
            await asyncio.sleep(0.15)
            assert not pending.done()
        finally:
            holder.close()
        try:
            response = await asyncio.wait_for(pending, timeout=3)
            assert response.status_code == 200
            assert threads and all(thread != owner for thread in threads)
            cached = Request({"type": "http", "method": "GET", "path": request.url.path, "headers": [(b"if-none-match", response.headers["etag"].encode())]})
            conditional = await thumbnail_media(cached, meme_id=str(record.id), services=provide_services, error=error)
            assert conditional.status_code == 304 and conditional.body == b""
        finally:
            event.remove(resources.engine, "before_cursor_execute", observe)

    asyncio.run(exercise())
