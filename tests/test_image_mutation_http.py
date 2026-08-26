"""公共图片重命名与删除 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
import backend.image_mutation_http as image_mutation_http
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError, Operations


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class _Metadata:
    """提供当前 scope 图片身份和可控副作用的 metadata service 替身。"""

    def __init__(self, source: Path, target_root: Path, *, revision: int = 7) -> None:
        self.source = source
        self.target_root = target_root
        self.record = SimpleNamespace(revision=revision)
        self.events: list[tuple[str, object]] = []
        self.rename_error: str | None = None
        self.remove_error: str | None = None
        self.blob_store = SimpleNamespace(resolve=self.resolve)

    def image_for_meme(self, meme_id: str):
        """返回当前 scope 的记录与受控源文件。"""
        self.events.append(("image_for_meme", meme_id))
        if meme_id == "missing":
            raise MetadataError("metadata_missing")
        return self.record, self.source

    def resolve(self, key: str, *, must_exist: bool = True) -> Path:
        """把业务 storage key 解析到测试根目录。"""
        self.events.append(("resolve", (key, must_exist)))
        target = self.target_root / key
        if must_exist and not target.exists():
            raise MetadataError("file_not_found")
        return target

    def rename_by_id(self, meme_id: str, target: Path):
        """记录重命名并返回旧 handler 形状的 metadata 结果。"""
        self.events.append(("rename_by_id", (meme_id, target)))
        if self.rename_error is not None:
            raise MetadataError(self.rename_error)
        return SimpleNamespace(image=SimpleNamespace(relative_path=target.name))

    def remove_by_id(self, meme_id: str) -> None:
        """记录删除或抛出受控 metadata 错误。"""
        self.events.append(("remove_by_id", meme_id))
        if self.remove_error is not None:
            raise MetadataError(self.remove_error)


def _request(query_params: dict[str, str] | None = None) -> SimpleNamespace:
    """构造只包含图片变更模块所需属性的最小 Request 替身。"""
    return SimpleNamespace(query_params=query_params or {})


def _payload(meme_id: str | None = "meme-1", new_name: str = "renamed") -> SimpleNamespace:
    """构造重命名/删除测试请求对象。"""
    return SimpleNamespace(meme_id=meme_id, new_name=new_name)


def _validator(value: str) -> str:
    """仅允许测试图片扩展名的业务 storage key 校验替身。"""
    if value.startswith((".", "invalid")) or value.endswith(".txt"):
        raise ValueError("invalid_storage_key")
    return value


def _operation_error(exc: OperationPolicyError) -> HTTPException:
    """把 policy 错误投影为稳定 HTTP 异常。"""
    return _error(403 if exc.code == "operation_forbidden" else 503, exc.code, str(exc))


def test_image_mutation_routes_keep_single_canonical_boundary() -> None:
    """重命名/删除 route 保留原 metadata 且不重复接管上传和只读入口。"""
    expected = {
        ("/images/rename", "POST", None),
        ("/images/delete", "POST", None),
    }
    actual = {
        (route.path, method, route.status_code)
        for route in api.app.routes
        for method in getattr(route, "methods", set())
        if route.path in {"/images/rename", "/images/delete"}
    }
    assert actual == expected
    assert all(route.tags == ["images"] for route in api.app.routes if route.path in {"/images/rename", "/images/delete"})
    for path in ("/images/upload", "/images", "/images/metadata", "/media/{meme_id}"):
        assert sum(1 for route in api.app.routes if route.path == path) == 1
    assert api.rename_image.__name__ == "rename_image"
    assert api.delete_image.__name__ == "delete_image"


def test_image_mutation_module_keeps_one_way_dependency_and_legacy_names() -> None:
    """新模块不反向导入入口，旧 handler 名称仍可用。"""
    source = Path(image_mutation_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported


def test_rename_uses_source_extension_and_invalidates_after_metadata(tmp_path: Path) -> None:
    """合法重命名沿用源扩展名，检索失效发生在 metadata 成功之后。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    events: list[str] = []
    result = asyncio.run(
        image_mutation_http.rename_image(
            _request(),
            _payload(new_name="renamed.jpg"),
            metadata_service=lambda _request: metadata,
            sanitize_filename=api._safe_filename,
            validate_storage_key=_validator,
            invalidate_search=lambda _request: events.append("invalidate"),
            error=_error,
        )
    )
    assert result == {"meme_id": "meme-1", "filename": "renamed.png", "media_url": "/media/meme-1"}
    assert [name for name, _value in metadata.events] == ["image_for_meme", "resolve", "rename_by_id"]
    assert events == ["invalidate"]


@pytest.mark.parametrize("new_name", ["../escape", "bad\nname", "invalid"])
def test_rename_rejects_unsafe_or_unsupported_target_without_metadata_write(tmp_path: Path, new_name: str) -> None:
    """路径/控制字符/不受支持扩展名不会进入 metadata rename 或检索失效。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    invalidated: list[str] = []
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_mutation_http.rename_image(
                _request(),
                _payload(new_name=new_name),
                metadata_service=lambda _request: metadata,
                sanitize_filename=api._safe_filename,
                validate_storage_key=_validator,
                invalidate_search=lambda _request: invalidated.append("invalidate"),
                error=_error,
            )
        )
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_filename"
    assert all(name != "rename_by_id" for name, _value in metadata.events)
    assert invalidated == []


def test_rename_target_conflict_is_checked_before_metadata_write(tmp_path: Path) -> None:
    """目标文件冲突映射为 409 且不调用 metadata rename。"""
    source = tmp_path / "source.png"
    target = tmp_path / "taken.png"
    source.write_bytes(b"source")
    target.write_bytes(b"taken")
    metadata = _Metadata(source, tmp_path)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_mutation_http.rename_image(
                _request(),
                _payload(new_name="taken"),
                metadata_service=lambda _request: metadata,
                sanitize_filename=api._safe_filename,
                validate_storage_key=_validator,
                invalidate_search=lambda _request: None,
                error=_error,
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (409, "file_exists")
    assert all(name != "rename_by_id" for name, _value in metadata.events)


def test_delete_commits_after_metadata_and_keeps_success_on_commit_failure(tmp_path: Path) -> None:
    """删除成功后 commit 异常不伪造失败，且不释放已完成 grant。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    events: list[object] = []

    def acquire(request, operation, key, **kwargs):
        """记录服务端可信 operation 事实。"""
        del request
        events.append(("acquire", operation, key, kwargs))
        return "grant-1"

    def commit(request, grant):
        """模拟 durable 删除后 policy 收束故障。"""
        del request
        events.append(("commit", grant))
        raise OperationPolicyError("operation_policy_unavailable")

    def release(request, grant):
        """记录不应发生的 release。"""
        del request
        events.append(("release", grant))

    result = asyncio.run(
        image_mutation_http.delete_image(
            _request(),
            _payload(),
            metadata_service=lambda _request: metadata,
            acquire_operation=acquire,
            commit_operation=commit,
            release_operation=release,
            operation_error=_operation_error,
            invalidate_search=lambda _request: events.append("invalidate"),
            error=_error,
        )
    )
    assert result == {"meme_id": "meme-1", "deleted": True}
    assert events[0][0] == "acquire"
    assert events[1] == ("commit", "grant-1")
    assert events[2] == "invalidate"
    assert all(event[0] != "release" for event in events if isinstance(event, tuple))


def test_delete_releases_only_known_pre_durable_metadata_failure(tmp_path: Path) -> None:
    """删除明确的副作用前 metadata 错误会尝试 release 并返回稳定失败。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    metadata.remove_error = "file_not_found"
    events: list[tuple[str, object]] = []
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_mutation_http.delete_image(
                _request(),
                _payload(),
                metadata_service=lambda _request: metadata,
                acquire_operation=lambda *_args, **_kwargs: events.append(("acquire", "grant")) or "grant-1",
                commit_operation=lambda *_args: events.append(("commit", "grant")),
                release_operation=lambda _request, grant: events.append(("release", grant)),
                operation_error=_operation_error,
                invalidate_search=lambda _request: events.append(("invalidate", "never")),
                error=_error,
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (500, "file_not_found")
    assert events == [("acquire", "grant"), ("release", "grant-1")]


def test_delete_cleanup_pending_commits_grant_invalidates_search_and_keeps_recoverable_error(tmp_path: Path) -> None:
    """durable 删除后的清理失败必须提交计量、失效搜索并返回可恢复错误。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    metadata.remove_error = "storage_cleanup_pending"
    events: list[tuple[str, object]] = []
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_mutation_http.delete_image(
                _request(),
                _payload(),
                metadata_service=lambda _request: metadata,
                acquire_operation=lambda *_args, **_kwargs: events.append(("acquire", "grant")) or "grant-1",
                commit_operation=lambda _request, grant: events.append(("commit", grant)),
                release_operation=lambda _request, grant: events.append(("release", grant)),
                operation_error=_operation_error,
                invalidate_search=lambda _request: events.append(("invalidate", "search")),
                error=_error,
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (503, "storage_cleanup_pending")
    assert events == [("acquire", "grant"), ("commit", "grant-1"), ("invalidate", "search")]


def test_delete_policy_rejection_happens_before_metadata_remove(tmp_path: Path) -> None:
    """policy acquire 拒绝时 fail-closed，metadata 删除和缓存失效均不发生。"""
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    metadata = _Metadata(source, tmp_path)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            image_mutation_http.delete_image(
                _request(),
                _payload(),
                metadata_service=lambda _request: metadata,
                acquire_operation=lambda *_args, **_kwargs: (_ for _ in ()).throw(OperationPolicyError("operation_forbidden")),
                commit_operation=lambda *_args: None,
                release_operation=lambda *_args: None,
                operation_error=_operation_error,
                invalidate_search=lambda _request: (_ for _ in ()).throw(AssertionError("cache must not invalidate")),
                error=_error,
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (403, "operation_forbidden")
    assert all(name != "remove_by_id" for name, _value in metadata.events)


@pytest.mark.parametrize("handler", [image_mutation_http.rename_image, image_mutation_http.delete_image])
def test_image_mutation_requires_meme_id_before_scope_service(tmp_path: Path, handler) -> None:
    """空 meme_id 在任何 scope metadata 或副作用 callback 前稳定拒绝。"""
    calls: list[str] = []
    kwargs = {
        "metadata_service": lambda _request: calls.append("metadata") or (_ for _ in ()).throw(AssertionError("metadata must not be read")),
        "invalidate_search": lambda _request: calls.append("invalidate"),
        "error": _error,
    }
    if handler is image_mutation_http.rename_image:
        kwargs.update(sanitize_filename=api._safe_filename, validate_storage_key=_validator)
    else:
        kwargs.update(
            acquire_operation=lambda *_args, **_kwargs: calls.append("acquire"),
            commit_operation=lambda *_args: calls.append("commit"),
            release_operation=lambda *_args: calls.append("release"),
            operation_error=_operation_error,
        )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(handler(_request(), _payload(meme_id=None), **kwargs))
    assert (caught.value.status_code, caught.value.detail["error"]) == (400, "meme_id_required")
    assert calls == []
