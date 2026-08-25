"""公共图片上传 HTTP 边界契约测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
from backend import image_upload_http
from backend.operation_policy import OperationPolicyError


class _Upload:
    """提供受控文件名和异步读取行为的 multipart 文件替身。"""

    def __init__(self, filename: str, content: bytes) -> None:
        """保存单文件输入和读取游标。"""
        self.filename = filename
        self._content = content
        self._read = False

    async def read(self, size: int) -> bytes:
        """按受限读取接口返回内容，第二次读取返回 EOF。"""
        if self._read:
            return b""
        self._read = True
        return self._content[:size]


class _Metadata:
    """记录 scope-bound metadata 访问和 durable 上传副作用。"""

    def __init__(self, root: Path, events: list[str]) -> None:
        """初始化测试文件根目录及事件记录。"""
        self.root = root
        self.events = events
        self.blob_store = SimpleNamespace(resolve=self.resolve)

    def resolve(self, key: str, *, must_exist: bool = False) -> Path:
        """返回当前 scope 根目录下的受控目标。"""
        self.events.append("resolve")
        return self.root / key

    def find_existing_upload(self, key: str, *, sha256: str, size_bytes: int):
        """默认表示目标尚未存在。"""
        self.events.append("find_existing_upload")
        return None

    def upload_bytes(self, content: bytes, *, target_key: str):
        """写入测试文件并返回旧 metadata service 形状。"""
        self.events.append("upload_bytes")
        path = self.root / target_key
        path.write_bytes(content)
        return "meme-1", path

    def status(self, image: Path) -> dict[str, str]:
        """返回稳定 metadata 状态。"""
        self.events.append("status")
        return {"status": "pending"}


def _request(tmp_path: Path, events: list[str], files: list[_Upload], *, fields: set[str] | None = None) -> SimpleNamespace:
    """构造只满足上传 callback 所需字段的最小 Request 替身。"""
    settings = SimpleNamespace(max_files_per_request=20, max_request_bytes=None, max_upload_size=1024)
    state = SimpleNamespace(settings=settings)
    request = SimpleNamespace(app=SimpleNamespace(state=state), fields=fields or {"files"}, events=events)
    request.form = SimpleNamespace(keys=lambda: request.fields, get=lambda key: None, getlist=lambda key: files if key == "files" else [])
    return request


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _options(*_args, **_kwargs) -> SimpleNamespace:
    """返回默认图片处理选项。"""
    return SimpleNamespace(reverse_image_policy="forbid", auto_name=False)


def _common_kwargs(request: SimpleNamespace, metadata: _Metadata, events: list[str], tmp_path: Path) -> dict[str, object]:
    """为模块函数注入最小、显式且 scope-bound 的依赖。"""
    request.form = SimpleNamespace(keys=lambda: request.fields, get=lambda key: None, getlist=lambda key: request._files if key == "files" else [])
    return {
        "settings": lambda received: received.app.state.settings,
        "metadata_service": lambda received: metadata,
        "task_service": lambda received: SimpleNamespace(seal_batch=lambda _batch: events.append("seal"), schedule=lambda _task: events.append("schedule")),
        "normalize_processing_options": _options,
        "parse_multipart_bool": lambda value, default=False: default if value is None else bool(value),
        "sanitize_filename": lambda value: value,
        "validate_image": lambda content, suffix: events.append("validate_image"),
        "calculate_sha256": lambda content: "a" * 64,
        "processing_worker": lambda received: None,
        "submit_processing_job": lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no_worker")),
        "processing_config": lambda received: {},
        "submit_visual_task": lambda *args, **kwargs: SimpleNamespace(task_id="task-1", status="pending"),
        "context_enqueue_error": lambda exc: "enqueue_failed",
        "acquire_operation": lambda *args, **kwargs: (events.append("acquire") or "grant"),
        "commit_operation": lambda received, grant: events.append("commit"),
        "release_operation": lambda received, grant: events.append("release"),
        "invalidate_search": lambda received: events.append("invalidate"),
        "error": _error,
    }


def test_upload_route_and_module_dependency_are_single_directional() -> None:
    """上传 canonical route 只注册一次，公共模块不反向依赖入口。"""
    matching = [route for route in api.app.routes if route.path == "/images/upload"]
    assert len(matching) == 1
    assert matching[0].methods == {"POST"}
    assert matching[0].tags == ["images"]
    source = Path(image_upload_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported
    assert api.upload_images.__name__ == "upload_images"
    assert api._parse_upload_form is image_upload_http._parse_upload_form
    assert api._read_upload_content is image_upload_http._read_upload_content


def test_upload_rejects_unknown_field_before_scope_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未知 multipart 字段在任何 metadata 或文件副作用前被拒绝。"""
    events: list[str] = []
    request = _request(tmp_path, events, [_Upload("a.png", b"png")], fields={"files", "directory"})
    request._files = [_Upload("a.png", b"png")]
    metadata = _Metadata(tmp_path, events)
    async def parse_form(*_args, **_kwargs):
        return request.form
    monkeypatch.setattr(image_upload_http, "_parse_upload_form", parse_form)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(image_upload_http.upload_images(request, **_common_kwargs(request, metadata, events, tmp_path)))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_request"
    assert events == []


def test_upload_commits_before_processing_and_search_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新图片必须按 acquire、durable、commit、任务和检索失效顺序收束。"""
    events: list[str] = []
    request = _request(tmp_path, events, [_Upload("a.png", b"png")])
    request._files = [_Upload("a.png", b"png")]
    metadata = _Metadata(tmp_path, events)
    async def parse_form(*_args, **_kwargs):
        return request.form
    monkeypatch.setattr(image_upload_http, "_parse_upload_form", parse_form)
    result = asyncio.run(image_upload_http.upload_images(request, **_common_kwargs(request, metadata, events, tmp_path)))
    assert result["results"][0]["ok"] is True
    assert events.index("find_existing_upload") < events.index("acquire") < events.index("upload_bytes") < events.index("commit")
    assert events.index("status") < events.index("invalidate")


def test_upload_policy_rejection_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """policy 拒绝不得触碰 durable upload。"""
    events: list[str] = []
    request = _request(tmp_path, events, [_Upload("a.png", b"png")])
    request._files = [_Upload("a.png", b"png")]
    metadata = _Metadata(tmp_path, events)
    async def parse_form(*_args, **_kwargs):
        return request.form
    monkeypatch.setattr(image_upload_http, "_parse_upload_form", parse_form)
    kwargs = _common_kwargs(request, metadata, events, tmp_path)
    kwargs["acquire_operation"] = lambda *args, **kwargs: (_ for _ in ()).throw(OperationPolicyError("operation_forbidden"))
    kwargs["operation_error"] = lambda exc: _error(403, exc.code, "forbidden")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(image_upload_http.upload_images(request, **kwargs))
    assert caught.value.status_code == 403
    assert "upload_bytes" not in events
    assert "commit" not in events
