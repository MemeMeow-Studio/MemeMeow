from __future__ import annotations

import asyncio
import cProfile
import hashlib
import os
import pstats
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine, delete, select
from starlette.requests import Request

from backend.collection_http import get_collection, list_collections
from backend.config import Settings
from backend.image_library_http import list_images
from backend.image_processing import ImageProcessingRepository
from backend.persistence.models import Meme, Scope, ScopeContext, Task
from backend.persistence.resources import DatabaseResources
from backend.scope import ScopeServiceFactory
from backend.services.thumbnails import ThumbnailError
from backend.visual import identity_from_settings


def test_lists_use_database_without_reading_images(tmp_path: Path) -> None:
    """使用真实 PostgreSQL 和图片验证列表、分页、合集及缩略图任务提交。"""
    url = os.environ.get("MEMEMEOW_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式提供开发测试 PostgreSQL 连接")
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=3)
    scope = ScopeContext(f"list-validation-{uuid4().hex}")
    resources = DatabaseResources(engine, image_root=tmp_path / "images", data_root=tmp_path, require_local_scope=False)
    settings = Settings(data_root=tmp_path, image_root=tmp_path / "images")
    factory = ScopeServiceFactory(resources, settings)
    try:
        with resources.factory() as session, session.begin():
            session.add(Scope(id=scope.scope_id))
        services = factory.for_scope(scope)
        records = []
        for index, color in enumerate(("red", "blue")):
            buffer = BytesIO()
            Image.new("RGB", (16, 8), color=color).save(buffer, format="PNG")
            content = buffer.getvalue()
            digest = hashlib.sha256(content).hexdigest()
            record = Meme(
                id=uuid4(), scope_id=scope.scope_id, storage_key=f"{digest}.png",
                display_name=f"list validation {index}", extension=".png",
                size_bytes=len(content), sha256=digest, context_status="pending",
                meme_context={"title": "测试图片"},
            )
            with resources.factory() as session, session.begin():
                session.add(record)
            source = services.metadata.blob_store.root / record.storage_key
            source.write_bytes(content)
            records.append(record)
        assert services.thumbnails.generate(records[0].id)["status"] == "available"
        thumbnail_path, _, _ = services.thumbnails.media_path(records[0].id)
        # 修改测试文件以确认列表展示数据库状态，媒体读取仍执行自身的检查。
        thumbnail_path.write_bytes(b"changed test thumbnail")
        for record in records:
            (services.metadata.blob_store.root / record.storage_key).write_bytes(b"changed test source")
        with resources.environment(scope) as environment:
            collection = environment.collections.create("list validation")
            environment.collections.add_members(collection.id, [record.id for record in records])
        request = Request({"type": "http", "method": "GET", "path": "/images", "query_string": b"", "headers": []})
        processing = ImageProcessingRepository(resources, scope)

        def environment_for_request(_request: Request):
            """向真实 HTTP handler 提供当前测试 scope 的数据库环境。"""
            return resources.environment(scope)

        def error(status: int, code: str, message: str) -> HTTPException:
            """按公共 API 格式构造错误响应。"""
            return HTTPException(status, detail={"error": code, "message": message})

        async def read_lists():
            """执行完整列表 handler，保留真实数据库服务和缩略图任务写入。"""
            images = await list_images(
                request, search="", page=1, page_size=10,
                services=lambda _request: services, environment=environment_for_request,
                processing_repository=lambda _request: processing,
                visual_identity=lambda _request: identity_from_settings(settings), error=error,
            )
            page = await list_images(
                request, search="", page=2, page_size=1,
                services=lambda _request: services, environment=environment_for_request,
                processing_repository=lambda _request: processing,
                visual_identity=lambda _request: identity_from_settings(settings), error=error,
            )
            detail = await get_collection(
                request, str(collection.id), page=1, page_size=10,
                environment=environment_for_request, metadata_service=lambda _request: services.metadata,
                thumbnail_service=lambda _request: services.thumbnails, error=error,
            )
            collections = await list_collections(
                request, page=1, page_size=10, environment=environment_for_request,
                thumbnail_service=lambda _request: services.thumbnails, error=error,
            )
            return images, page, detail, collections

        profiler = cProfile.Profile()
        images, page, detail, collections = profiler.runcall(asyncio.run, read_lists())
        file_operations = {"image_sha256", "_identity", "_read_identity", "exists_with_identity", "_output_is_valid", "read_bytes"}
        file_modules = {"metadata.py", "thumbnails.py", "storage.py"}
        observed = {(filename, name) for (filename, _line, name) in pstats.Stats(profiler).stats if "/repositories/" not in filename and Path(filename).name in file_modules and name in file_operations}
        assert not observed
        assert images["total"] == 2
        assert len(images["items"]) == 2
        assert {item["thumbnail"]["status"] for item in images["items"]} == {"available", "pending"}
        assert page["total"] == 2 and len(page["items"]) == 1
        assert detail["total"] == 2 and len(detail["members"]) == 2
        assert all(member["metadata"]["title"] == "测试图片" for member in detail["members"])
        expected = {str(record.id): record for record in records}
        for member in detail["members"]:
            record = expected[member["meme_id"]]
            assert member["display_name"] == record.display_name
            assert member["filename"] == member["saved_filename"] == f"{record.display_name}.png"
            assert member["extension"] == ".png" and member["size"] == record.size_bytes
            assert member["media_url"] == f"/media/{record.id}"
        assert collections["total"] == 1
        assert "cover_thumbnail" in collections["items"][0]
        with resources.factory() as session:
            tasks = list(session.scalars(select(Task).where(Task.scope_id == scope.scope_id)))
            assert tasks
            assert all(task.task_type == "derived_thumbnail_generation" for task in tasks)
            assert all(task.payload["meme_id"] == str(records[1].id) for task in tasks)
        # 媒体读取以持久化内容版本为准，文件完整性由维护流程检查。
        assert services.thumbnails.media_path(records[0].id)[0] == thumbnail_path
    finally:
        factory.shutdown()
        with resources.factory() as session, session.begin():
            session.execute(delete(Scope).where(Scope.id == scope.scope_id))
        engine.dispose()
