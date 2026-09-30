# 使用真实 GIF 和 gifsicle 核验大小、动画时间信息、透明背景以及资源错误。

from io import BytesIO
from pathlib import Path
import random
from time import monotonic, sleep
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from PIL import Image
from sqlalchemy import delete

from backend.gif_compression import (
    MAX_GIF_OUTPUT_BYTES, MAX_GIF_UPLOAD_BYTES, GifCompressionError,
    compress_gif, gif_preflight_root, gif_upload_limit, scan_gif,
)
from backend.image_safety import ImagePreflightError, preflight_upload_image
from backend.persistence.file_lock import shared_file_lock
from backend.persistence.engine import create_engine_for_url
from backend.persistence.models import Scope, ScopeContext
from backend.persistence.resources import DatabaseResources
from backend.services.tasks import PostgresTaskService


def animated_gif(size: tuple[int, int], count: int, *, loop: int | None = 0, transparent: bool = False, transparent_rows: int = 1) -> bytes:
    """生成真实的逐帧随机动画，尺寸、时长、循环和透明边界供压缩验证使用。"""
    generator = random.Random(42)
    frames = []
    palette = [channel for color in range(256) for channel in (color, color, color)]
    for index in range(count):
        frame = Image.frombytes("P", size, generator.randbytes(size[0] * size[1]))
        frame.putpalette(palette)
        if transparent:
            for y in range(transparent_rows):
                for x in range(size[0]):
                    frame.putpixel((x, y), 0)
            frame.info["transparency"] = 0
        frames.append(frame)
    output = BytesIO()
    options = {"loop": loop} if loop is not None else {}
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[10 + index % 5 * 10 for index in range(count)], disposal=2, optimize=False, **options)
    for frame in frames:
        frame.close()
    return output.getvalue()


@pytest.fixture(scope="module")
def large_animation() -> bytes:
    """提供超过 10MB 且超过 100 帧的可解码动画，实际执行压缩。"""
    content = animated_gif((320, 320), 560, transparent=True, transparent_rows=32)
    assert len(content) > 64 * 1024 * 1024
    assert MAX_GIF_OUTPUT_BYTES < len(content) < MAX_GIF_UPLOAD_BYTES
    return content


def test_large_upload_preserves_animation_and_is_deterministic(large_animation: bytes) -> None:
    """实际压缩后符合 10MB，动画快照不变，相同输入产生相同最终指纹。"""
    first = preflight_upload_image(large_animation, ".gif")
    second = preflight_upload_image(large_animation, ".gif")
    assert first.content == second.content
    assert len(first.content) <= MAX_GIF_OUTPUT_BYTES
    with Image.open(BytesIO(large_animation)) as image:
        original = scan_gif(image)
    with Image.open(BytesIO(first.content)) as image:
        final = scan_gif(image)
    assert final.durations == original.durations
    assert final.loop == original.loop
    assert final.transparent is True
    assert final.transparent_frames == original.transparent_frames


@pytest.mark.parametrize("loop", [None, 0, 3])
def test_small_animation_keeps_original_bytes(loop: int | None) -> None:
    """小于 10MB 的多帧 GIF 保留全部原始内容，包括有限和无限循环。"""
    content = animated_gif((3, 3), 130, loop=loop)
    assert preflight_upload_image(content, ".gif").content == content


def test_repeated_compression_scales_without_deleting_frames(tmp_path: Path) -> None:
    """较小目标真实触发多次缩放，保留每帧时间和有限循环。"""
    content = animated_gif((96, 96), 12, loop=2, transparent=True, transparent_rows=24)
    compressed = compress_gif(content, tmp_path, max_output_bytes=4000)
    assert len(compressed) <= 4000
    with Image.open(BytesIO(content)) as image:
        original = scan_gif(image)
    with Image.open(BytesIO(compressed)) as image:
        final = scan_gif(image)
    assert final.size[0] < original.size[0]
    assert final.durations == original.durations
    assert final.loop == 2
    assert final.transparent
    assert final.transparent_frames == original.transparent_frames


def test_transparency_loss_is_rejected(tmp_path: Path) -> None:
    """透明边界缩放后消失时，有限尝试结束并明确报告透明信息变化。"""
    content = animated_gif((96, 96), 12, loop=2, transparent=True)
    with pytest.raises(GifCompressionError) as error:
        compress_gif(content, tmp_path, max_output_bytes=4000)
    assert error.value.code == "gif_compression_transparency_changed"


def test_unreachable_target_returns_specific_error(tmp_path: Path) -> None:
    """目标小于 GIF 本身结构所需空间时耗尽有限尝试并明确报错。"""
    content = animated_gif((3, 3), 2)
    with pytest.raises(GifCompressionError) as error:
        compress_gif(content, tmp_path, max_output_bytes=1)
    assert error.value.code == "gif_compression_target_exceeded"


def test_receiving_limit_uses_content_and_restores_cursor() -> None:
    """真实 GIF 采用 100MB 接收上限，普通图片使用原配置，读取不改变游标。"""
    gif = BytesIO(animated_gif((3, 3), 2))
    assert gif_upload_limit(gif, 20) == MAX_GIF_UPLOAD_BYTES
    assert gif.tell() == 0
    png = BytesIO()
    Image.new("RGB", (3, 3)).save(png, format="PNG")
    png.seek(0)
    assert gif_upload_limit(png, 20) == 20
    assert png.tell() == 0


def test_busy_processing_returns_specific_error() -> None:
    """使用真实共享锁占用两个处理位置，第三个 GIF 明确返回并发限制。"""
    root = gif_preflight_root()
    with shared_file_lock(root / "slot-0.lock"), shared_file_lock(root / "slot-1.lock"):
        with pytest.raises(ImagePreflightError) as error:
            preflight_upload_image(animated_gif((3, 3), 2), ".gif")
    assert error.value.code == "gif_processing_busy"


def test_tiny_gif_deadline_cleans_up_worker_files() -> None:
    """真实截止时间终止 GIF worker，临时目录由父进程清理。"""
    root = gif_preflight_root()
    before = set(root.glob("image-*"))
    with pytest.raises(ImagePreflightError) as error:
        preflight_upload_image(animated_gif((3, 3), 2), ".gif", timeout_seconds=0.001)
    assert error.value.code == "gif_compression_timeout"
    assert set(root.glob("image-*")) == before


@pytest.mark.parametrize("error_code", ["gif_processing_busy", "invalid_image"])
def test_upload_error_is_final_task_error(tmp_path: Path, error_code: str) -> None:
    """真实锁占满或图片损坏时，PostgreSQL Task 保留原因并以一次 attempt 结束。"""
    engine = create_engine_for_url(dotenv_values(".env")["MEMEMEOW_DATABASE_URL"])
    scope = ScopeContext("test-gif-error-" + uuid4().hex)
    with engine.begin() as connection:
        connection.execute(Scope.__table__.insert().values(id=scope.scope_id))
    resources = DatabaseResources(engine, image_root=tmp_path / "images", data_root=tmp_path / "data", require_local_scope=False)
    tasks = PostgresTaskService(resources, scope_id=scope)
    content = animated_gif((3, 3), 2) if error_code == "gif_processing_busy" else b"invalid image"

    def handler(payload, progress):
        """执行生产图片预检，由真实共享锁或解码错误触发指定终态。"""
        preflight_upload_image(content, ".gif")
        return {"validated": True}

    tasks.register("image_upload", handler)
    try:
        root = gif_preflight_root()
        with shared_file_lock(root / "slot-0.lock"), shared_file_lock(root / "slot-1.lock"):
            accepted = tasks.submit("image_upload", {"receipt_id": uuid4().hex, "filename": "test-gif-error.gif", "batch_id": uuid4().hex})
            deadline = monotonic() + 10
            while monotonic() < deadline:
                task = tasks.get(accepted.task_id)
                if task.status in {"succeeded", "failed"}:
                    break
                sleep(0.02)
            assert task.status == "failed", task.error
            assert task.error["error"] == error_code
            assert task.attempts == 1
    finally:
        tasks.shutdown()
        with engine.begin() as connection:
            connection.execute(delete(Scope).where(Scope.id == scope.scope_id))
        engine.dispose()


def test_flashing_animation_keeps_every_brightness_change(tmp_path: Path) -> None:
    """真实黑白频闪压缩后保留每次亮度变化及逐帧时长。"""
    frames = [Image.new("RGB", (128, 128), "white" if index % 2 else "black") for index in range(120)]
    output = BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[10 + index % 3 * 10 for index in range(120)], loop=4)
    compressed = compress_gif(output.getvalue(), tmp_path, max_output_bytes=4000)
    with Image.open(BytesIO(compressed)) as image:
        assert image.n_frames == 120
        assert image.info["loop"] == 4
        for index in range(120):
            image.seek(index)
            assert image.info["duration"] == 10 + index % 3 * 10
            expected = 255 if index % 2 else 0
            assert image.convert("RGB").getpixel((image.width // 2, image.height // 2)) == (expected, expected, expected)
