"""公共图片库只读 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

import api
import backend.image_library_http as image_library_http
from backend.metadata import MetadataError


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class _Environment:
    """提供图片列表/视觉状态读取的最小 scope 环境夹具。"""

    def __init__(self, records: list[object], visual: object | None = None) -> None:
        self.memes = SimpleNamespace(list=lambda **_kwargs: records, count=lambda **_kwargs: len(records))
        self.visual = SimpleNamespace(get=lambda *_args, **_kwargs: visual)

    def __enter__(self) -> "_Environment":
        """返回当前测试环境。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束测试环境，不吞掉异常。"""
        del exc_type, exc, traceback


class _Metadata:
    """提供受控 BlobStore、身份和 metadata 详情的测试服务。"""

    def __init__(self, image, *, status: dict[str, object] | None = None) -> None:
        self.image = image
        self.status_value = status or {"status": "ready", "title": "标题"}
        self.blob_store = SimpleNamespace(resolve=lambda _key: image)

    def _identity(self, _image) -> dict[str, object]:
        """返回已验证文件身份。"""
        return {"size_bytes": 12}

    def status(self, _image) -> dict[str, object]:
        """返回图片状态摘要。"""
        return dict(self.status_value)

    def image_for_meme(self, meme_id: str):
        """返回测试 Meme 与受控文件路径。"""
        if meme_id == "missing":
            raise MetadataError("metadata_missing")
        return SimpleNamespace(id=meme_id), self.image

    def load(self, _image):
        """返回可投影的 metadata 对象。"""
        return SimpleNamespace(model_dump=lambda **_kwargs: {"image": {"relative_path": "meme.png"}, "context_status": "ready"})


def _services(image, *, status: dict[str, object] | None = None, cache: bool = True) -> SimpleNamespace:
    """组装当前 scope 的 metadata/search facade。"""
    return SimpleNamespace(metadata=_Metadata(image, status=status), search=SimpleNamespace(has_cache=lambda: cache))


def _request() -> SimpleNamespace:
    """构造只包含 query 参数的最小 Request 替身。"""
    return SimpleNamespace(query_params={})


def _call_list(request, records, environment, services, processing=None):
    """调用图片列表模块并注入全部宿主依赖。"""
    return asyncio.run(
        image_library_http.list_images(
            request,
            search="",
            page=1,
            page_size=50,
            services=lambda _request: services,
            environment=lambda _request: environment,
            processing_repository=lambda _request: processing or SimpleNamespace(latest_for_target=lambda *_args: None),
            visual_identity=lambda _request: SimpleNamespace(model="visual", preprocess_version="v1", dimensions=768),
            error=_error,
        )
    )


def test_image_library_routes_and_legacy_names_remain_available() -> None:
    """图片列表、详情和媒体 route metadata 保持兼容且不重复注册。"""
    expected = {"/images", "/images/metadata", "/media/{meme_id}"}
    for path in expected:
        routes = [route for route in api.app.routes if getattr(route, "path", None) == path]
        assert len(routes) == 1
        assert routes[0].methods == {"GET"}
        assert routes[0].tags == ["images"]
    assert api.list_images.__name__ == "list_images"
    assert api.image_metadata.__name__ == "image_metadata"
    assert api.media.__name__ == "media"
    route_paths = [getattr(route, "path", None) for route in api.app.routes]
    assert route_paths.index("/images/processing") < route_paths.index("/images")


def test_image_library_module_keeps_one_way_dependency() -> None:
    """图片库模块不得反向导入入口模块。"""
    source = Path(image_library_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported


def test_image_list_projects_status_and_processing_summary(tmp_path: Path) -> None:
    """列表输出稳定图片字段、三类状态和最新处理摘要。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    record = SimpleNamespace(id=uuid4(), storage_key="meme.png", extension=".png", sha256="a" * 64)
    processing = SimpleNamespace(latest_for_target=lambda *_args: SimpleNamespace(as_dict=lambda: {"job_id": "job-1", "status": "failed", "auto_name": True, "has_warnings": True, "stages": []}))
    payload = _call_list(_request(), [record], _Environment([record], visual=object()), _services(image), processing)
    item = payload["items"][0]
    assert item["meme_id"] == str(record.id)
    assert item["media_url"] == f"/media/{record.id}"
    assert item["embedding_status"] == "ready"
    assert item["visual_embedding_status"] == "ready"
    assert item["processing_job_id"] == "job-1"
    assert payload["total"] == 1


def test_image_list_reuses_source_identity_for_thumbnail_projection_and_metadata(tmp_path: Path) -> None:
    """图片列表对同一原图只计算一次 SHA，并把身份传给缩略图和 metadata。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    record = SimpleNamespace(
        id=uuid4(),
        storage_key="meme.png",
        extension=".png",
        sha256=hashlib.sha256(b"image-content").hexdigest(),
    )

    class CountingMetadata(_Metadata):
        """记录列表请求中的原图身份计算与复用。"""

        def __init__(self, source):
            """初始化身份读取计数。"""
            super().__init__(source)
            self.identity_calls = 0
            self.received_identity = None

        def _identity(self, _image):
            """返回完整文件身份并记录调用次数。"""
            self.identity_calls += 1
            return {"size_bytes": 13, "sha256": record.sha256, "extension": ".png", "relative_path": "meme.png"}

        def status(self, _image, *, identity=None):
            """接收列表已验证的身份，避免再次读取文件。"""
            self.received_identity = identity
            return {"status": "ready", "title": "标题"}

    metadata = CountingMetadata(image)
    projections: list[dict[object, tuple[int, str]]] = []
    services = SimpleNamespace(
        metadata=metadata,
        search=SimpleNamespace(has_cache=lambda: True),
        thumbnails=SimpleNamespace(
            projections=lambda _records, *, source_identities: projections.append(source_identities) or {record.id: {"status": "pending", "media_url": None}}
        ),
    )
    payload = _call_list(_request(), [record], _Environment([record], visual=None), services)
    assert payload["items"][0]["thumbnail"]["status"] == "pending"
    assert metadata.identity_calls == 1
    assert metadata.received_identity is not None
    assert projections == [{record.id: (13, record.sha256)}]


def test_image_list_supports_legacy_thumbnail_projection_signature(tmp_path: Path) -> None:
    """图片列表仍可调用只接受 records positional 参数的旧缩略图 facade。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    record = SimpleNamespace(id=uuid4(), storage_key="meme.png", extension=".png", sha256="a" * 64)
    calls: list[list[object]] = []
    services = _services(image)
    services.thumbnails = SimpleNamespace(
        projections=lambda records: calls.append(records) or {record.id: {"status": "pending", "media_url": None}}
    )

    payload = _call_list(_request(), [record], _Environment([record], visual=None), services)

    assert payload["items"][0]["thumbnail"]["status"] == "pending"
    assert calls == [[record]]


@pytest.mark.parametrize("selector", ["directory", "scope_id", "user_id"])
def test_image_list_rejects_unknown_path_selector(selector: str) -> None:
    """目录/scope/user 等旧选择器不能进入图片列表 repository。"""
    request = _request()
    request.query_params = {selector: "secret"}
    with pytest.raises(HTTPException) as caught:
        _call_list(request, [], _Environment([]), _services(Path("/tmp/missing.png")))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_request"


def test_image_metadata_requires_id_and_projects_missing_error(tmp_path: Path) -> None:
    """metadata 详情拒绝空 ID，并把未知 Meme 投影为稳定 404。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    services = _services(image)
    request = _request()
    with pytest.raises(HTTPException) as required:
        asyncio.run(image_library_http.image_metadata(request, meme_id=None, services=lambda _request: services, error=_error))
    assert required.value.detail["error"] == "meme_id_required"
    with pytest.raises(HTTPException) as missing:
        asyncio.run(image_library_http.image_metadata(request, meme_id="missing", services=lambda _request: services, error=_error))
    assert missing.value.status_code == 404
    assert missing.value.detail["error"] == "meme_not_found"


def test_image_metadata_and_media_use_meme_id_and_verified_path(tmp_path: Path) -> None:
    """详情附带稳定 meme_id，媒体返回经过 service 校验的 PNG response。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    services = _services(image)
    request = _request()
    metadata = asyncio.run(image_library_http.image_metadata(request, meme_id="meme-1", services=lambda _request: services, error=_error))
    assert metadata["meme_id"] == "meme-1"
    response = asyncio.run(image_library_http.media(request, meme_id="meme-1", services=lambda _request: services, error=_error))
    assert isinstance(response, FileResponse)
    assert response.media_type == "image/png"


def test_image_media_maps_metadata_failure_to_not_found(tmp_path: Path) -> None:
    """媒体路径指纹/记录错误只投影稳定 not-found。"""
    image = tmp_path / "meme.png"
    image.write_bytes(b"image-content")
    services = _services(image)
    services.metadata.image_for_meme = lambda _meme_id: (_ for _ in ()).throw(MetadataError("metadata_image_mismatch"))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(image_library_http.media(_request(), meme_id="meme-1", services=lambda _request: services, error=_error))
    assert caught.value.status_code == 404
    assert caught.value.detail["error"] == "meme_not_found"
