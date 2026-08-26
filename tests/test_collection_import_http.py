"""公共合集 ZIP 导入 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import api
import backend.collection_import_http as collection_import_http
from backend.collection_packages import CollectionPackageError, MAX_ARCHIVE_COMPRESSED_BYTES, MAX_TOTAL_UNCOMPRESSED_BYTES
from backend.database import DatabaseError
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError, Operations


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


class _Form(dict[str, object]):
    """提供合集 handler 所需的 ``getlist`` multipart 替身。"""

    def getlist(self, key: str) -> list[object]:
        """按字段返回单个测试上传对象。"""
        return list(self.get(key, [])) if isinstance(self.get(key), list) else [self[key]] if key in self else []


class _Upload:
    """记录上传文件关闭事实的异步替身。"""

    filename = "import.zip"

    def __init__(self) -> None:
        self.closed = False

    async def read(self, _size: int = -1) -> bytes:
        """提供 multipart 上传对象所需的异步读取接口。"""
        return b"archive"

    async def close(self) -> None:
        """记录预检结束后的资源关闭。"""
        self.closed = True


class _Collections:
    """记录当前 scope 合集 repository 的导入调用。"""

    def __init__(self, collection: SimpleNamespace) -> None:
        self.collection = collection
        self.calls: list[tuple[str, object]] = []

    def by_name(self, name: str) -> None:
        """模拟当前 scope 不存在同名合集。"""
        self.calls.append(("by_name", name))
        return None

    def create(self, name: str) -> SimpleNamespace:
        """创建并返回测试合集。"""
        self.calls.append(("create", name))
        return self.collection

    def add_members(self, collection_id: object, meme_ids: list[str]) -> None:
        """记录成员关系写入。"""
        self.calls.append(("add_members", (collection_id, meme_ids)))


class _Environment:
    """提供当前 scope 的合集和 Meme repository context manager。"""

    def __init__(self, collections: _Collections, records: list[object]) -> None:
        self.collections = collections
        self.memes = SimpleNamespace(list_all=lambda: records)

    def __enter__(self) -> "_Environment":
        """返回 scope-bound 测试环境。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束测试事务且不吞掉异常。"""
        del exc_type, exc, traceback


def _package(*, source_id: str = "source-1", filename: str = "cat.png", sha256: str = "a" * 64) -> SimpleNamespace:
    """构造已经通过包预检的单成员合集包。"""
    member = SimpleNamespace(source_meme_id=source_id, filename_at_export=filename, sha256=sha256)
    manifest = SimpleNamespace(collection=SimpleNamespace(name="导入合集"))
    return SimpleNamespace(manifest=manifest, members=(SimpleNamespace(manifest=member, content=b"png-bytes"),))


def _request() -> SimpleNamespace:
    """构造不启动 ASGI lifespan 的最小请求对象。"""
    return SimpleNamespace(query_params={})


def _call_import(monkeypatch: pytest.MonkeyPatch, *, package: SimpleNamespace, environment: _Environment, events: list[object], acquire=None, metadata=None, worker=None, thumbnail_enqueue=None):
    """注入最小宿主边界并调用合集导入 handler。"""
    upload = _Upload()

    async def parse_upload_form(*_args, **_kwargs):
        """返回受控单文件 multipart 表单。"""
        return _Form(file=upload)

    monkeypatch.setattr(collection_import_http, "_parse_upload_form", parse_upload_form)

    async def read_upload(_upload, *, max_upload_size: int):
        """返回受控包内容并核对压缩字节预算。"""
        events.append(("read", max_upload_size))
        return b"archive", False

    monkeypatch.setattr(collection_import_http, "_read_upload_content", read_upload)
    monkeypatch.setattr(collection_import_http, "preflight_archive", lambda content, **kwargs: events.append(("preflight", content, kwargs)) or package)
    metadata = metadata or SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: False),
        upload_bytes=lambda content, *, target_key: events.append(("upload", content, target_key)) or ("target-1", Path("/scope") / target_key),
    )
    acquire = acquire or (lambda request, operation, key, **kwargs: events.append(("acquire", operation, key, kwargs)) or "grant-1")
    return asyncio.run(
        collection_import_http.import_collection(
            _request(),
            environment=lambda _request: events.append("environment") or environment,
            metadata_service=lambda _request: metadata,
            settings=lambda _request: SimpleNamespace(max_upload_size=1024 * 1024),
            processing_worker=lambda _request: worker,
            processing_config=lambda _request: {"model": "test"},
            submit_visual_task=lambda _request, path, **kwargs: events.append(("visual", path, kwargs)) or SimpleNamespace(task_id="visual-1", status="queued"),
            thumbnail_enqueue=thumbnail_enqueue,
            context_enqueue_error=lambda exc: "context_enqueue_failed",
            acquire_operation=acquire,
            commit_operation=lambda _request, grant: events.append(("commit", grant)),
            release_operation=lambda _request, grant: events.append(("release", grant)),
            invalidate_search=lambda _request: events.append("invalidate"),
            error=_error,
            database_error=lambda exc: _error(503, exc.code, "数据库错误"),
        )
    ), upload


def test_collection_import_routes_keep_single_canonical_boundary() -> None:
    """导入、导出和 CRUD route 保留唯一注册及公开 metadata。"""
    routes = [route for route in api.app.routes if route.path == "/collections/import" and route.methods == {"POST"}]
    assert len(routes) == 1
    assert routes[0].status_code is None
    assert routes[0].tags == ["collections"]
    assert sum(1 for route in api.app.routes if route.path == "/collections/{collection_id}/export") == 1
    assert api.import_collection.__name__ == "import_collection"
    assert sum(1 for route in api.app.routes if route.path == "/collections" and route.methods == {"GET"}) == 1
    assert sum(1 for route in api.app.routes if route.path == "/collections" and route.methods == {"POST"}) == 1


def test_collection_import_module_keeps_one_way_dependency_and_no_routes() -> None:
    """新模块不得反向导入入口或自行注册 FastAPI route。"""
    source = Path(collection_import_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported
    assert "from fastapi import FastAPI" not in source


def test_collection_import_preserves_budget_and_side_effect_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """预检预算先于数据库，新增成员按 acquire、写入、commit、关系、任务顺序完成。"""
    collection = SimpleNamespace(id=uuid4())
    collections = _Collections(collection)
    environment = _Environment(collections, [])
    events: list[object] = []
    result, upload = _call_import(monkeypatch, package=_package(), environment=environment, events=events)
    assert result["status"] == "succeeded"
    item = result["results"][0]
    assert item["ok"] is True
    assert item["status"] == "imported"
    assert result["meme_id_map"] == {"source-1": "target-1"}
    assert upload.closed is True
    assert events[0] == ("read", MAX_ARCHIVE_COMPRESSED_BYTES)
    assert events[1][0] == "preflight"
    assert events.index(("acquire", Operations.IMAGE_UPLOAD, "upload:" + "a" * 64 + ":cat.png", {"resource_id": "cat.png", "source": "collection_import", "input_digest": "a" * 64})) < events.index(("upload", b"png-bytes", "cat.png"))
    assert events.index(("upload", b"png-bytes", "cat.png")) < events.index(("commit", "grant-1"))
    assert events.index(("commit", "grant-1")) < events.index("invalidate")
    assert ("visual", Path("/scope/cat.png"), {"expected_sha256": "a" * 64, "schedule": True}) in events
    assert MAX_TOTAL_UNCOMPRESSED_BYTES > 0


def test_collection_import_rejects_preflight_error_before_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """包 SHA/path 预检失败时不打开 scope environment 或获取 operation。"""
    upload = _Upload()

    async def parse_upload_form(*_args, **_kwargs):
        """返回受控单文件 multipart 表单。"""
        return _Form(file=upload)

    monkeypatch.setattr(collection_import_http, "_parse_upload_form", parse_upload_form)

    async def read_upload(_upload, *, max_upload_size: int):
        """返回受控压缩内容。"""
        del max_upload_size
        return b"archive", False

    monkeypatch.setattr(collection_import_http, "_read_upload_content", read_upload)

    def reject_preflight(content: bytes, **kwargs):
        """模拟 manifest 路径与 SHA 不一致。"""
        del content, kwargs
        raise CollectionPackageError("sha256_mismatch")

    monkeypatch.setattr(collection_import_http, "preflight_archive", reject_preflight)
    called: list[str] = []
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            collection_import_http.import_collection(
                _request(),
                environment=lambda _request: called.append("environment"),
                metadata_service=lambda _request: called.append("metadata"),
                settings=lambda _request: SimpleNamespace(max_upload_size=1024),
                processing_worker=lambda _request: None,
                processing_config=lambda _request: {},
                submit_visual_task=lambda *_args, **_kwargs: called.append("visual"),
                context_enqueue_error=lambda _exc: "failed",
                acquire_operation=lambda *_args, **_kwargs: called.append("acquire"),
                commit_operation=lambda *_args: called.append("commit"),
                release_operation=lambda *_args: called.append("release"),
                invalidate_search=lambda _request: called.append("invalidate"),
                error=_error,
                database_error=lambda exc: _error(503, exc.code, "数据库错误"),
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (400, "sha256_mismatch")
    assert called == []
    assert upload.closed is True


def test_collection_import_policy_rejection_is_member_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """单成员 policy 拒绝只生成该项错误，不写文件或失效检索。"""
    collection = SimpleNamespace(id=uuid4())
    environment = _Environment(_Collections(collection), [])
    events: list[object] = []

    def reject(*_args, **_kwargs):
        """拒绝可信上传 operation。"""
        raise OperationPolicyError("operation_forbidden")

    result, _upload = _call_import(monkeypatch, package=_package(), environment=environment, events=events, acquire=reject)
    item = result["results"][0]
    assert result["status"] == "partial"
    assert item["error"] == "operation_forbidden"
    assert item["ok"] is False
    assert result["meme_id_map"] == {"source-1": ""}
    assert all(not (isinstance(event, tuple) and event[0] in {"upload", "commit"}) for event in events)
    assert "invalidate" not in events


def test_collection_import_reuses_identity_checked_meme_without_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """同名同 SHA 图片只建立合集关系，不重复写文件或获取 upload grant。"""
    collection = SimpleNamespace(id=uuid4())
    existing = SimpleNamespace(id=uuid4(), storage_key="cat.png", sha256="a" * 64, size_bytes=9)
    environment = _Environment(_Collections(collection), [existing])
    events: list[object] = []
    metadata = SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: True),
        upload_bytes=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reused image must not upload")),
    )
    result, _upload = _call_import(monkeypatch, package=_package(), environment=environment, events=events, metadata=metadata)
    item = result["results"][0]
    assert item["status"] == "reused"
    assert item["target_meme_id"] == str(existing.id)
    assert result["meme_id_map"] == {"source-1": str(existing.id)}
    assert not any(isinstance(event, tuple) and event[0] == "acquire" for event in events)
    assert "invalidate" not in events


@pytest.mark.parametrize("reused", [False, True])
def test_collection_import_enqueues_thumbnail_for_imported_and_reused_meme(monkeypatch: pytest.MonkeyPatch, reused: bool) -> None:
    """新导入和同名复用都必须走同一个幂等缩略图 enqueue callback。"""
    collection = SimpleNamespace(id=uuid4())
    existing = [SimpleNamespace(id=uuid4(), storage_key="cat.png", sha256="a" * 64, size_bytes=9)] if reused else []
    environment = _Environment(_Collections(collection), existing)
    events: list[object] = []
    metadata = SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: reused),
        upload_bytes=lambda content, *, target_key: events.append(("upload", content, target_key)) or ("target-1", Path("/scope") / target_key),
    )
    result, _upload = _call_import(
        monkeypatch,
        package=_package(),
        environment=environment,
        events=events,
        metadata=metadata,
        thumbnail_enqueue=lambda _request, meme_id: events.append(("thumbnail", meme_id)),
    )
    item = result["results"][0]
    expected_id = str(existing[0].id) if reused else "target-1"
    assert item["target_meme_id"] == expected_id
    assert ("thumbnail", expected_id) in events


def test_collection_import_thumbnail_enqueue_failure_is_a_warning_after_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """缩略图任务失败只记录 warning，不回滚已导入 Meme 和合集关系。"""
    collection = SimpleNamespace(id=uuid4())
    collections = _Collections(collection)
    environment = _Environment(collections, [])
    events: list[object] = []

    def fail_enqueue(_request, _meme_id):
        """模拟 durable 导入后缩略图任务不可用。"""
        raise RuntimeError("thumbnail_task_unavailable")

    result, _upload = _call_import(
        monkeypatch,
        package=_package(),
        environment=environment,
        events=events,
        thumbnail_enqueue=fail_enqueue,
    )
    item = result["results"][0]
    assert result["status"] == "partial"
    assert item["ok"] is True
    assert item["thumbnail_enqueue_error"] == "thumbnail_task_unavailable"
    assert any(event[0] == "add_members" for event in collections.calls if isinstance(event, tuple))


def test_collection_import_uses_sha_suffix_for_same_name_different_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """同名异 SHA 图片沿用包 helper 的安全 SHA 后缀命名。"""
    collection = SimpleNamespace(id=uuid4())
    existing = SimpleNamespace(id=uuid4(), storage_key="cat.png", sha256="b" * 64, size_bytes=9)
    environment = _Environment(_Collections(collection), [existing])
    events: list[object] = []
    metadata = SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: True),
        upload_bytes=lambda content, *, target_key: events.append(("upload", content, target_key)) or ("target-2", Path("/scope") / target_key),
    )
    result, _upload = _call_import(
        monkeypatch,
        package=_package(sha256="a" * 64),
        environment=environment,
        events=events,
        metadata=metadata,
    )
    assert result["results"][0]["status"] == "imported"
    assert ("upload", b"png-bytes", "cat-aaaaaaaa.png") in events


def test_collection_import_commit_failure_keeps_durable_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """durable 写入后 commit 不确定时保留成功事实且不 release grant。"""
    collection = SimpleNamespace(id=uuid4())
    environment = _Environment(_Collections(collection), [])
    events: list[object] = []

    def commit(_request, grant):
        """模拟写入后的 policy 收束故障。"""
        events.append(("commit", grant))
        raise OperationPolicyError("operation_policy_unavailable")

    upload = _Upload()

    async def parse_upload_form(*_args, **_kwargs):
        """返回受控单文件 multipart 表单。"""
        return _Form(file=upload)

    monkeypatch.setattr(collection_import_http, "_parse_upload_form", parse_upload_form)

    async def read_upload(_upload, *, max_upload_size: int):
        """返回受控压缩内容。"""
        del max_upload_size
        return b"archive", False

    monkeypatch.setattr(collection_import_http, "_read_upload_content", read_upload)
    monkeypatch.setattr(collection_import_http, "preflight_archive", lambda *_args, **_kwargs: _package())
    metadata = SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: False),
        upload_bytes=lambda content, *, target_key: events.append(("upload", content, target_key)) or ("target-1", Path("/scope") / target_key),
    )
    result = asyncio.run(
        collection_import_http.import_collection(
            _request(),
            environment=lambda _request: environment,
            metadata_service=lambda _request: metadata,
            settings=lambda _request: SimpleNamespace(max_upload_size=1024),
            processing_worker=lambda _request: None,
            processing_config=lambda _request: {},
            submit_visual_task=lambda *_args, **_kwargs: SimpleNamespace(task_id="visual-1", status="queued"),
            context_enqueue_error=lambda _exc: "failed",
            acquire_operation=lambda *_args, **_kwargs: "grant-1",
            commit_operation=commit,
            release_operation=lambda _request, grant: events.append(("release", grant)),
            invalidate_search=lambda _request: events.append("invalidate"),
            error=_error,
            database_error=lambda exc: _error(503, exc.code, "数据库错误"),
        )
    )
    assert result["results"][0]["ok"] is True
    assert result["status"] == "succeeded"
    assert ("release", "grant-1") not in events
    assert "invalidate" in events


def test_collection_import_known_pre_durable_failure_releases_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """明确的 staging 错误只释放 reservation，不报告图片已写入。"""
    collection = SimpleNamespace(id=uuid4())
    environment = _Environment(_Collections(collection), [])
    events: list[object] = []
    metadata = SimpleNamespace(
        blob_store=SimpleNamespace(exists_with_identity=lambda *_args, **_kwargs: False),
        upload_bytes=lambda *_args, **_kwargs: (_ for _ in ()).throw(MetadataError("staging_conflict")),
    )
    result, _upload = _call_import(monkeypatch, package=_package(), environment=environment, events=events, metadata=metadata)
    assert result["status"] == "partial"
    assert result["results"][0]["error"] == "staging_conflict"
    assert ("release", "grant-1") in events
    assert all(not (isinstance(event, tuple) and event[0] in {"commit", "visual"}) for event in events)


def test_collection_import_database_error_uses_injected_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """scope repository 的业务错误使用入口注入的稳定错误工厂。"""
    upload = _Upload()

    async def parse_upload_form(*_args, **_kwargs):
        """返回受控单文件 multipart 表单。"""
        return _Form(file=upload)

    monkeypatch.setattr(collection_import_http, "_parse_upload_form", parse_upload_form)

    async def read_upload(_upload, *, max_upload_size: int):
        """返回受控压缩内容。"""
        del max_upload_size
        return b"archive", False

    monkeypatch.setattr(collection_import_http, "_read_upload_content", read_upload)
    monkeypatch.setattr(collection_import_http, "preflight_archive", lambda *_args, **_kwargs: _package())
    database_error = DatabaseError("collection_exists")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            collection_import_http.import_collection(
                _request(),
                environment=lambda _request: (_ for _ in ()).throw(database_error),
                metadata_service=lambda _request: SimpleNamespace(),
                settings=lambda _request: SimpleNamespace(max_upload_size=1024),
                processing_worker=lambda _request: None,
                processing_config=lambda _request: {},
                submit_visual_task=lambda *_args, **_kwargs: None,
                context_enqueue_error=lambda _exc: "failed",
                acquire_operation=lambda *_args, **_kwargs: None,
                commit_operation=lambda *_args: None,
                release_operation=lambda *_args: None,
                invalidate_search=lambda _request: None,
                error=_error,
                database_error=lambda exc: _error(409, exc.code, "合集名称已存在"),
            )
        )
    assert (caught.value.status_code, caught.value.detail["error"]) == (409, "collection_exists")
