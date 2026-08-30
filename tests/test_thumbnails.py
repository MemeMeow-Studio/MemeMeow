"""缩略图派生规则、完整性投影和异常资源清理测试。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from backend.persistence.engine import DatabaseError
from backend.persistence.models import ScopeContext
from backend.services.thumbnails import DerivedThumbnailService, RenderedThumbnail, ThumbnailError, _render_thumbnail
from backend.thumbnail_config import ThumbnailConfig


def _image_bytes(size: tuple[int, int], *, mode: str = "RGB", color: object = "red", image_format: str = "PNG") -> bytes:
    """生成测试图片字节，避免测试依赖仓库外部媒体文件。"""
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class _FileStore:
    """提供缩略图服务单元测试所需的最小受控文件 API。"""

    def __init__(self, root: Path) -> None:
        """绑定测试文件根目录。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, key: str, *, must_exist: bool = True) -> Path:
        """按测试 key 返回根目录内路径。"""
        path = self.root / key
        if must_exist and not path.is_file():
            raise DatabaseError("file_not_found")
        return path

    def _key_path(self, key: str, *, must_exist: bool = False) -> Path:
        """解析普通和暂存 key。"""
        path = self.root / key
        if must_exist and not path.is_file():
            raise DatabaseError("file_not_found")
        return path

    def exists_with_identity(self, key: str, *, sha256: str | None = None, size_bytes: int | None = None) -> bool:
        """检查测试文件存在并按需复核 SHA/大小。"""
        path = self._key_path(key)
        if not path.is_file():
            return False
        content = path.read_bytes()
        return (size_bytes is None or len(content) == size_bytes) and (sha256 is None or hashlib.sha256(content).hexdigest() == sha256)

    def stage_bytes(self, content: bytes, *, token) -> str:
        """写入测试暂存文件并返回内部 key。"""
        path = self._key_path(f".staging/{token.hex}.part")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f".staging/{token.hex}.part"

    def link_move(self, source_key: str, target_key: str) -> None:
        """模拟同文件系统的暂存文件落位。"""
        source = self._key_path(source_key, must_exist=True)
        target = self._key_path(target_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def unlink(self, key: str) -> None:
        """删除测试受控文件。"""
        path = self._key_path(key, must_exist=True)
        path.unlink()


class _ThumbnailRepository:
    """在内存中保存一条当前缩略图事实。"""

    def __init__(self, row=None) -> None:
        """初始化可选派生事实。"""
        self.row = row
        self.deleted = False
        self.fail_available = False

    def current(self, _meme, _profile, *, for_update: bool = False):
        """返回当前测试事实。"""
        del for_update
        return self.row

    def list_current(self, memes, _profile):
        """返回测试页中与当前 Meme 绑定的派生事实。"""
        if self.row is None:
            return {}
        return {meme.id: self.row for meme in memes if meme.id == self.row.meme_id}

    def ensure_pending(self, meme, profile, *, reset_failed: bool = False):
        """幂等创建 pending 测试事实。"""
        if self.row is None:
            self.row = SimpleNamespace(
                meme_id=meme.id,
                source_sha256=str(meme.sha256).lower(),
                source_size_bytes=meme.size_bytes,
                profile=profile,
                output_key=None,
                output_sha256=None,
                output_size_bytes=None,
                width=None,
                height=None,
                media_type=None,
                status="pending",
            )
        elif reset_failed and self.row.status in {"failed", "stale"}:
            self.row.status = "pending"
            self.row.output_key = None
            self.row.output_sha256 = None
            self.row.output_size_bytes = None
            self.row.width = None
            self.row.height = None
            self.row.media_type = None
        return self.row

    def mark_pending(self, meme, profile):
        """恢复测试事实为 pending。"""
        return self.ensure_pending(meme, profile, reset_failed=True)

    def mark_available(self, meme, profile, **values):
        """提交测试输出，或模拟数据库提交失败。"""
        if self.fail_available:
            raise DatabaseError("thumbnail_finalize_failed")
        row = self.ensure_pending(meme, profile)
        for key, value in values.items():
            setattr(row, key, value)
        row.status = "available"
        return row

    def mark_failed(self, meme, profile, diagnostic):
        """记录测试失败状态。"""
        row = self.ensure_pending(meme, profile)
        row.status = "failed"
        row.diagnostic = diagnostic
        return row

    def mark_stale(self, _meme_id, _profile, *, diagnostic=None):
        """记录测试 stale 状态。"""
        if self.row is not None:
            self.row.status = "stale"
            self.row.diagnostic = diagnostic

    def delete_for_meme(self, _meme_id):
        """删除测试事实。"""
        self.deleted = True
        self.row = None


class _Environment:
    """提供共享 repository/session 的测试环境上下文。"""

    def __init__(self, meme, thumbnails):
        """绑定 Meme 和缩略图 repository。"""
        self.memes = SimpleNamespace(get=lambda _meme_id, **_kwargs: meme, list=lambda **_kwargs: [meme])
        self.thumbnails = thumbnails
        session = SimpleNamespace(flush=lambda: None)
        session.scalars = lambda _statement: [thumbnails.row] if thumbnails.row is not None else []
        self.uow = SimpleNamespace(session=session)

    def __enter__(self):
        """进入测试环境。"""
        return self

    def __exit__(self, *_args):
        """退出测试环境。"""
        return None


class _Resources:
    """拼装原图和缩略图测试存储。"""

    def __init__(self, meme, source_store, thumbnail_store, repository):
        """绑定测试 scope 资源。"""
        self.meme = meme
        self.source_store = source_store
        self.thumbnail_store = thumbnail_store
        self.environment_instance = _Environment(meme, repository)

    def environment(self, _scope):
        """返回固定测试环境。"""
        return self.environment_instance

    def blob_store_for_scope(self, _scope):
        """返回原图测试存储。"""
        return self.source_store

    def thumbnail_store_for_scope(self, _scope):
        """返回隔离的缩略图测试存储。"""
        return self.thumbnail_store


def _service(tmp_path: Path, content: bytes, row=None):
    """创建绑定一张测试 Meme 的缩略图服务。"""
    source_store = _FileStore(tmp_path / "source")
    thumbnail_store = _FileStore(tmp_path / "thumbnail")
    (source_store.root / "image.png").write_bytes(content)
    meme = SimpleNamespace(
        id=uuid4(),
        storage_key="image.png",
        extension=".png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    repository = _ThumbnailRepository(row)
    resources = _Resources(meme, source_store, thumbnail_store, repository)
    service = DerivedThumbnailService(resources, scope_id=ScopeContext("local"), config=ThumbnailConfig(concurrency=1, backpressure=1))
    return service, meme, repository, source_store, thumbnail_store


def test_render_thumbnail_keeps_ratio_and_does_not_upscale() -> None:
    """高分辨率图片缩小且保持比例，小图片不被放大。"""
    config = ThumbnailConfig()
    large = _render_thumbnail(_image_bytes((640, 320)), ".png", config)
    small = _render_thumbnail(_image_bytes((100, 80)), ".png", config)
    assert (large.width, large.height) == (320, 160)
    assert (small.width, small.height) == (100, 80)


def test_render_thumbnail_preserves_alpha_and_uses_gif_first_frame() -> None:
    """透明 PNG 保留 RGBA，动画 GIF 只输出第一帧。"""
    transparent = _render_thumbnail(_image_bytes((12, 8), mode="RGBA", color=(255, 0, 0, 80)), ".png", ThumbnailConfig())
    with Image.open(BytesIO(transparent.content)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 80

    first = Image.new("RGB", (8, 8), "red")
    second = Image.new("RGB", (8, 8), "blue")
    output = BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=10, loop=0)
    rendered = _render_thumbnail(output.getvalue(), ".gif", ThumbnailConfig())
    with Image.open(BytesIO(rendered.content)) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_render_thumbnail_enforces_temp_and_output_limits() -> None:
    """生成器在解码临时像素和输出字节超限时 fail closed。"""
    content = _image_bytes((20, 20))
    with pytest.raises(ThumbnailError, match="thumbnail_temp_limit_exceeded"):
        _render_thumbnail(content, ".png", ThumbnailConfig(max_temp_bytes=100))
    with pytest.raises(ThumbnailError, match="thumbnail_output_limit_exceeded"):
        _render_thumbnail(content, ".png", ThumbnailConfig(max_output_bytes=1))


def test_projection_rejects_changed_source_and_invalid_output(tmp_path: Path) -> None:
    """原图外部替换或派生文件损坏时不再投影媒体地址。"""
    content = _image_bytes((32, 16))
    service, meme, repository, source_store, thumbnail_store = _service(tmp_path, content)
    output = b"thumbnail"
    key = service._output_key(meme, service.config.profile)
    (thumbnail_store.root / key).write_bytes(output)
    repository.row = SimpleNamespace(
        meme_id=meme.id,
        source_sha256=meme.sha256,
        source_size_bytes=meme.size_bytes,
        profile=service.config.profile,
        output_key=key,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_size_bytes=len(output),
        width=32,
        height=16,
        media_type="image/png",
        status="available",
    )
    assert service.projection(meme)["status"] == "available"
    source_store.resolve("image.png").write_bytes(b"changed")
    projection = service.projection(meme)
    assert projection == {"status": "stale", "media_url": None}
    assert repository.row.status == "stale"

    source_store.resolve("image.png").write_bytes(content)
    (thumbnail_store.root / key).write_bytes(b"corrupt")
    projection = service.projection(meme)
    assert projection == {"status": "stale", "media_url": None}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_key", "not-the-derived-key.png"),
        ("output_sha256", "0" * 64),
        ("output_size_bytes", 1),
        ("width", 321),
        ("height", 0),
        ("media_type", None),
        ("media_type", "image/jpeg"),
    ],
)
def test_projection_rejects_invalid_output_metadata(tmp_path: Path, field: str, value: object) -> None:
    """输出 key、尺寸、媒体类型或指纹不完整时必须阻断 available 投影。"""
    content = _image_bytes((32, 16))
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, content)
    output = b"thumbnail"
    key = service._output_key(meme, service.config.profile)
    (thumbnail_store.root / key).write_bytes(output)
    values = {
        "meme_id": meme.id,
        "source_sha256": meme.sha256,
        "source_size_bytes": meme.size_bytes,
        "profile": service.config.profile,
        "output_key": key,
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_size_bytes": len(output),
        "width": 32,
        "height": 16,
        "media_type": "image/png",
        "status": "available",
    }
    values[field] = value
    repository.row = SimpleNamespace(**values)

    assert service.projection(meme) == {"status": "stale", "media_url": None}
    assert repository.row.status == "stale"


def test_generate_removes_new_output_when_fact_finalize_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """派生文件已落位但事实提交失败时只回收本次新文件。"""
    content = _image_bytes((16, 8))
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, content)
    monkeypatch.setattr("backend.services.thumbnails.validate_image_content", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.services.thumbnails._render_thumbnail", lambda *_args, **_kwargs: RenderedThumbnail(b"rendered", 16, 8))
    repository.fail_available = True
    with pytest.raises(ThumbnailError, match="thumbnail_finalize_failed"):
        service.generate(meme.id)
    assert not (thumbnail_store.root / service._output_key(meme, service.config.profile)).exists()


def test_generate_treats_same_fingerprint_target_race_as_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个 Worker 同时安装同一输出时，后到者不会把成功事实标成 failed。"""
    content = _image_bytes((16, 8))
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, content)
    monkeypatch.setattr("backend.services.thumbnails.validate_image_content", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.services.thumbnails._render_thumbnail", lambda *_args, **_kwargs: RenderedThumbnail(b"rendered", 16, 8))
    key = service._output_key(meme, service.config.profile)
    (thumbnail_store.root / key).write_bytes(b"rendered")
    original_exists = thumbnail_store.exists_with_identity
    first_target_check = True

    def race_exists(check_key: str, **kwargs: object) -> bool:
        """让首次目标检查看到空目录，随后模拟另一 Worker 已落位。"""
        nonlocal first_target_check
        if check_key == key and first_target_check:
            first_target_check = False
            return False
        return original_exists(check_key, **kwargs)

    monkeypatch.setattr(thumbnail_store, "exists_with_identity", race_exists)
    projection = service.generate(meme.id)
    assert projection["status"] == "available"
    assert repository.row is not None and repository.row.status == "available"
    assert not list((thumbnail_store.root / ".staging").glob("*"))


def test_failure_does_not_preserve_corrupt_available_fact(tmp_path: Path) -> None:
    """损坏的 available 事实不能阻止失败路径改写为可重试的 failed。"""
    content = _image_bytes((16, 8))
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, content)
    key = service._output_key(meme, service.config.profile)
    output = b"corrupt-output"
    (thumbnail_store.root / key).write_bytes(output)
    repository.row = SimpleNamespace(
        meme_id=meme.id,
        source_sha256=meme.sha256,
        source_size_bytes=meme.size_bytes,
        profile=service.config.profile,
        output_key=key,
        output_sha256=hashlib.sha256(b"different-output").hexdigest(),
        output_size_bytes=len(b"different-output"),
        width=16,
        height=8,
        media_type="image/png",
        status="available",
    )

    service._mark_failure(meme, "thumbnail_generation_failed")

    assert repository.row.status == "failed"


def test_projection_reuses_verified_source_identity_without_rereading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """列表已完成原图身份校验时，缩略图投影不重复读取 SHA。"""
    content = _image_bytes((16, 8))
    service, meme, _repository, _source_store, _thumbnail_store = _service(tmp_path, content)
    monkeypatch.setattr(service, "_identity", lambda _path: (_ for _ in ()).throw(AssertionError("identity should be reused")))
    projection = service.projection(meme, source_identity=(meme.size_bytes, meme.sha256))
    assert projection == {"status": "pending", "media_url": None}


def test_projection_schedules_missing_thumbnail_after_fact_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """图片列表首次投影存量 Meme 时，pending 事实提交后会唤醒去重任务。"""
    content = _image_bytes((16, 8))
    service, meme, _repository, _source_store, _thumbnail_store = _service(tmp_path, content)
    scheduled: list[UUID | str] = []
    monkeypatch.setattr(service, "task_service", object())
    monkeypatch.setattr(service, "enqueue", lambda meme_id: scheduled.append(meme_id) or object())

    projection = service.projections([meme], source_identities={meme.id: (meme.size_bytes, meme.sha256)})

    assert projection[meme.id] == {"status": "pending", "media_url": None}
    assert scheduled == [meme.id]


def test_single_projection_schedules_missing_thumbnail_for_collection_and_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """合集或检索的单图投影也会为没有历史任务的 Meme 触发生成。"""
    content = _image_bytes((16, 8))
    service, meme, _repository, _source_store, _thumbnail_store = _service(tmp_path, content)
    scheduled: list[UUID | str] = []
    monkeypatch.setattr(service, "task_service", object())
    monkeypatch.setattr(service, "enqueue", lambda meme_id: scheduled.append(meme_id) or object())

    projection = service.projection(meme, source_identity=(meme.size_bytes, meme.sha256))

    assert projection == {"status": "pending", "media_url": None}
    assert scheduled == [meme.id]


def test_reconcile_resets_failed_thumbnail_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """受保护回填会清除失败状态，允许存量图片重新提交生成任务。"""
    content = _image_bytes((16, 8))
    service, meme, repository, _source_store, _thumbnail_store = _service(tmp_path, content)
    repository.row = repository.ensure_pending(meme, service.config.profile)
    repository.row.status = "failed"
    calls: list[tuple[UUID | str, bool]] = []
    monkeypatch.setattr(service, "task_service", object())
    monkeypatch.setattr(service, "enqueue", lambda meme_id, *, reset_failed=False: calls.append((meme_id, reset_failed)) or object())

    result = service.reconcile()

    assert result == {"scanned": 1, "submitted": 1, "available": 0, "failed": 0}
    assert calls == [(meme.id, True)]


def test_media_path_only_validates_source_identity_and_does_not_use_generation_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """媒体读取只校验原图身份，不调用会加载原图字节的生成读取路径。"""
    content = _image_bytes((16, 8))
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, content)
    output = b"thumbnail"
    key = service._output_key(meme, service.config.profile)
    (thumbnail_store.root / key).write_bytes(output)
    repository.row = SimpleNamespace(
        meme_id=meme.id,
        source_sha256=meme.sha256,
        source_size_bytes=meme.size_bytes,
        profile=service.config.profile,
        output_key=key,
        output_sha256=hashlib.sha256(output).hexdigest(),
        output_size_bytes=len(output),
        width=16,
        height=8,
        media_type="image/png",
        status="available",
    )
    monkeypatch.setattr(service, "_meme_and_source", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("generation reader must not run")))
    path, media_type = service.media_path(meme.id)
    assert path.name == key
    assert media_type == "image/png"


def test_cleanup_rejects_invalid_uuid_and_preserves_facts_on_file_failure(tmp_path: Path) -> None:
    """清理入口统一返回领域错误，物理失败时不先删除数据库事实。"""
    service, meme, repository, _source_store, thumbnail_store = _service(tmp_path, _image_bytes((8, 8)))
    with pytest.raises(ThumbnailError, match="meme_not_found"):
        service.cleanup_for_meme("not-a-uuid")

    key = service._output_key(meme, service.config.profile)
    repository.row = repository.ensure_pending(meme, service.config.profile)
    repository.row.output_key = key
    repository.row.status = "available"
    (thumbnail_store.root / key).write_bytes(b"output")
    original_unlink = thumbnail_store.unlink
    thumbnail_store.unlink = lambda _key: (_ for _ in ()).throw(DatabaseError("file_delete_failed"))
    with pytest.raises(ThumbnailError, match="thumbnail_cleanup_failed"):
        service.cleanup_for_meme(meme.id)
    assert repository.row is not None
    thumbnail_store.unlink = original_unlink
