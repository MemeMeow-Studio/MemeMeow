# 使用真实文件和 ASGI 请求验证通用文件条件响应。

import hashlib

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.http_file_cache import revalidated_file_response


@pytest.fixture
def file_client(tmp_path):
    """创建真实文件和文件响应路由，内容版本只在写入时计算。"""
    path = tmp_path / "asset.bin"
    content = b"media-cache-test-v1"
    path.write_bytes(content)
    state = {"digest": hashlib.sha256(content).hexdigest()}

    async def endpoint(request: Request):
        """让实际 ASGI 请求经过生产文件响应函数。"""
        return await revalidated_file_response(
            request, path, media_type="application/octet-stream", content_digest=state["digest"],
        )

    app = Starlette(routes=[Route("/asset", endpoint, methods=["GET", "HEAD"])])
    with TestClient(app) as client:
        yield client, path, state


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("condition", ["strong", "weak", "list", "wildcard", "repeated"])
def test_matching_condition_returns_empty_304(file_client, method, condition):
    """强标签、弱标签、列表、通配符和重复条件头均按 GET 弱比较规则处理。"""
    client, _, state = file_client
    etag = f'"{state["digest"]}"'
    values = {
        "strong": etag,
        "weak": f"W/{etag}",
        "list": f'"unrelated", W/{etag}',
        "wildcard": "*",
        "repeated": '"unrelated"',
    }
    headers = [("If-None-Match", values[condition])]
    if condition == "repeated":
        headers.append(("If-None-Match", etag))
    response = client.request(method, "/asset", headers=headers)
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie"
    assert "content-length" not in response.headers


def test_changed_content_returns_new_bytes_and_etag(file_client):
    """同一 URL 的持久化内容版本变化后返回新内容，即使文件大小相同。"""
    client, path, state = file_client
    first = client.get("/asset")
    assert first.status_code == 200
    assert first.content == path.read_bytes()
    changed = b"media-cache-test-v2"
    path.write_bytes(changed)
    state["digest"] = hashlib.sha256(changed).hexdigest()
    second = client.get("/asset", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200
    assert second.content == changed
    assert second.headers["etag"] != first.headers["etag"]
    third = client.get("/asset", headers={"If-None-Match": second.headers["etag"]})
    assert third.status_code == 304 and third.content == b""


def test_etag_takes_precedence_over_modification_date(file_client):
    """版本不匹配时，未来的修改日期条件不能让客户端继续使用旧内容。"""
    client, path, _ = file_client
    response = client.get("/asset", headers={
        "If-None-Match": '"old-version"',
        "If-Modified-Since": "Wed, 21 Oct 2099 07:28:00 GMT",
    })
    assert response.status_code == 200
    assert response.content == path.read_bytes()


def test_range_requests_preserve_content_version(file_client):
    """范围下载保持相同内容版本；条件命中时优先返回 304。"""
    client, path, _ = file_client
    first = client.get("/asset")
    response = client.get("/asset", headers={"Range": "bytes=0-4", "If-Range": first.headers["etag"]})
    assert response.status_code == 206
    assert response.content == path.read_bytes()[:5]
    assert response.headers["etag"] == first.headers["etag"]
    cached = client.get("/asset", headers={"Range": "bytes=0-4", "If-None-Match": first.headers["etag"]})
    assert cached.status_code == 304 and cached.content == b""


def test_missing_file_cannot_return_304(file_client):
    """实际文件缺失时保留文件错误，媒体入口负责转换为 HTTP 错误。"""
    client, path, _ = file_client
    first = client.get("/asset")
    path.unlink()
    with pytest.raises(FileNotFoundError):
        client.get("/asset", headers={"If-None-Match": first.headers["etag"]})
