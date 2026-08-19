"""上传 multipart 字节预算和逐项 spool 读取测试。"""

from __future__ import annotations

import asyncio
from inspect import getsource

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import _parse_upload_form, _read_upload_content, upload_images


class _RecordingUpload:
    """记录单文件读取调用，验证下一个 spool 不会提前读取。"""

    def __init__(self, content: bytes) -> None:
        """保存测试内容和当前读取位置。"""
        self.content = content
        self.position = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        """按调用方要求返回下一段字节并记录读取大小。"""
        self.read_sizes.append(size)
        if self.position >= len(self.content):
            return b""
        end = min(len(self.content), self.position + size)
        chunk = self.content[self.position:end]
        self.position = end
        return chunk


def _multipart_request(files: list[tuple[str, bytes]], *, chunk_size: int = 11) -> Request:
    """构造分块 ASGI multipart 请求，不设置 Content-Length。"""
    boundary = b"upload-boundary"
    parts: list[bytes] = []
    for filename, content in files:
        parts.extend(
            (
                b"--" + boundary + b"\r\n",
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode(),
                b"Content-Type: image/png\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    body = b"".join(parts) + b"--" + boundary + b"--\r\n"
    chunks = [body[index:index + chunk_size] for index in range(0, len(body), chunk_size)]

    async def receive() -> dict[str, object]:
        """向 Starlette 逐段提供请求体。"""
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    content_type = b"multipart/form-data; boundary=" + boundary
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/images/upload",
        "raw_path": b"/images/upload",
        "query_string": b"",
        "headers": [(b"content-type", content_type)],
    }
    return Request(scope, receive)


def test_upload_stream_reads_one_spool_before_touching_the_next() -> None:
    """逐个读取 helper 只触碰当前文件，并在超限时只读 max_upload_size+1。"""
    first = _RecordingUpload(b"a" * 7)
    second = _RecordingUpload(b"b" * 20)

    first_content, first_too_large = asyncio.run(_read_upload_content(first, max_upload_size=7))
    assert first_content == b"a" * 7
    assert first_too_large is False
    assert first.read_sizes
    assert second.read_sizes == []

    del first_content
    second_content, second_too_large = asyncio.run(_read_upload_content(second, max_upload_size=7))
    assert second_content == b"b" * 8
    assert second_too_large is True
    assert second.read_sizes[0] == 8

    # 路由不应恢复 preloaded 列表式全请求缓存；顺序读取必须发生在逐文件循环内。
    route_source = getsource(upload_images)
    assert "preloaded" not in route_source
    assert "await _read_upload_content" in route_source


def test_multipart_budget_accepts_exact_file_bytes_without_content_length() -> None:
    """文件字节总量恰好等于预算时 parser 允许请求返回 form。"""
    files = [("a.png", b"a" * 5), ("b.png", b"b" * 7)]
    form = asyncio.run(_parse_upload_form(_multipart_request(files), max_files=21, max_request_bytes=12))
    uploads = form.getlist("files")
    assert len(uploads) == 2
    for upload in uploads:
        upload.file.close()


def test_multipart_budget_rejects_before_form_returns_or_durable_handling() -> None:
    """文件总量越过预算时 parser 在返回 form 前以 413 拒绝。"""
    request = _multipart_request([("a.png", b"a" * 5), ("b.png", b"b" * 8)])
    with pytest.raises(HTTPException) as error:
        asyncio.run(_parse_upload_form(request, max_files=21, max_request_bytes=12))
    assert error.value.status_code == 413
    assert error.value.detail["error"] == "request_too_large"
