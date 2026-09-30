# 普通上传的 GIF 压缩与共享动画验证；所有格式读取均使用 Pillow 和 gifsicle。

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from io import BytesIO
import os
from pathlib import Path
import resource
import signal
import subprocess
import time
import tempfile
from typing import BinaryIO, Iterator

from loguru import logger
from PIL import Image

from backend.persistence.file_lock import shared_file_lock


MAX_GIF_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_GIF_OUTPUT_BYTES = 10 * 1024 * 1024
GIF_PROCESSING_TIMEOUT_SECONDS = 120.0
GIF_PROCESS_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_GIF_FRAME_PIXELS = 25_000_000
GIF_ERROR_CODES = frozenset({
    "gif_processing_busy", "gif_processing_memory_exceeded", "gif_compression_timeout",
    "gif_compression_tool_missing", "gif_compression_encoder_failed", "gif_compression_output_limit_exceeded",
    "gif_compression_animation_changed", "gif_compression_transparency_changed", "gif_compression_target_exceeded",
})


class GifCompressionError(ValueError):
    """GIF 读取或压缩异常；code 用于任务状态和用户提示。"""

    def __init__(self, code: str):
        """保存可公开的错误类别，具体工具诊断记录在服务日志中。"""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GifAnimation:
    """动画验证快照：画布尺寸、每帧时长、循环次数和实际透明像素。"""

    size: tuple[int, int]
    durations: tuple[int, ...]
    loop: int | None
    transparent: bool
    transparent_frames: tuple[bool, ...]


def scan_gif(image: Image.Image) -> GifAnimation:
    """逐帧解码 GIF，返回动画快照；供上传和已有图片验证共用。"""
    if image.format != "GIF":
        raise GifCompressionError("invalid_image")
    size = image.size
    if size[0] * size[1] > MAX_GIF_FRAME_PIXELS:
        raise GifCompressionError("image_frame_pixels_exceeded")
    loop = image.info.get("loop")
    durations = []
    transparent_frames = []
    try:
        # 使用 Pillow 的逐帧读取，资源限制由外层独立进程的内存和时间预算执行。
        for index in range(image.n_frames):
            image.seek(index)
            if image.width * image.height > MAX_GIF_FRAME_PIXELS:
                raise GifCompressionError("image_frame_pixels_exceeded")
            image.load()
            durations.append(int(image.info.get("duration", 0)))
            transparent = False
            if "transparency" in image.info or image.mode == "RGBA":
                rgba = image.convert("RGBA")
                try:
                    transparent = rgba.getchannel("A").getextrema()[0] == 0
                finally:
                    rgba.close()
            transparent_frames.append(transparent)
    except GifCompressionError:
        raise
    except MemoryError as exc:
        raise GifCompressionError("gif_processing_memory_exceeded") from exc
    except Exception as exc:
        raise GifCompressionError("invalid_image") from exc
    if not durations:
        raise GifCompressionError("invalid_image")
    return GifAnimation(size, tuple(durations), loop, any(transparent_frames), tuple(transparent_frames))


def limit_gif_worker() -> None:
    """隔离 GIF worker 的进程组并限制内存，超时可同时终止外部编码器。"""
    os.setsid()
    resource.setrlimit(resource.RLIMIT_AS, (GIF_PROCESS_MEMORY_BYTES, GIF_PROCESS_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * MAX_GIF_UPLOAD_BYTES, 2 * MAX_GIF_UPLOAD_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def gif_upload_limit(source: BinaryIO, ordinary_limit: int) -> int:
    """根据文件实际签名选择接收上限，读完后恢复文件游标；不信任文件名和 MIME。"""
    position = source.tell()
    try:
        signature = source.read(6)
    finally:
        source.seek(position)
    return MAX_GIF_UPLOAD_BYTES if signature in {b"GIF87a", b"GIF89a"} else ordinary_limit


@contextmanager
def gif_preflight_resources(is_gif: bool) -> Iterator[Path]:
    """父进程持有并发位置及临时目录，启动失败和超时均能释放资源。"""
    root = Path.cwd() / "temp" / "gif-compression"
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources:
        if is_gif:
            for index in range(2):
                if resources.enter_context(shared_file_lock(root / f"slot-{index}.lock", blocking=False)):
                    break
            else:
                raise GifCompressionError("gif_processing_busy")
        directory = resources.enter_context(tempfile.TemporaryDirectory(prefix="image-", dir=root))
        yield Path(directory)


def compress_gif(content: bytes, directory: Path, *, max_output_bytes: int = MAX_GIF_OUTPUT_BYTES) -> bytes:
    """在已隔离的 worker 内从原始 GIF 重试压缩，返回保持动画时间信息的最终字节。"""
    if len(content) > MAX_GIF_UPLOAD_BYTES:
        raise GifCompressionError("file_too_large")
    with Image.open(BytesIO(content)) as image:
        original = scan_gif(image)
    output_limit = min(MAX_GIF_OUTPUT_BYTES, max_output_bytes)
    if len(content) <= output_limit:
        return content
    source = directory / "source.gif"
    target = directory / "compressed.gif"
    diagnostics = directory / "gifsicle.log"
    source.write_bytes(content)
    started = time.monotonic()
    # 每次都从原始文件编码；先调整颜色，再逐步缩小尺寸，保留全部动画帧。
    profiles = [(256, 20, 1.0), (128, 40, 1.0), (64, 80, 1.0)]
    profiles.extend((64, 80, scale) for scale in (0.8, 0.6, 0.4, 0.25, 0.125, 0.0625, 0.015625))
    profiles.append((2, 100, 0.0))
    validation_error = None
    for attempt, (colors, lossy, scale) in enumerate(profiles, 1):
        remaining = GIF_PROCESSING_TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise GifCompressionError("gif_compression_timeout")
        width = max(1, int(original.size[0] * scale))
        height = max(1, int(original.size[1] * scale))
        command = [
            "gifsicle", "--no-ignore-errors", "--conserve-memory", "--threads=1",
            "--optimize=3", "--optimize=keep-empty", "--no-comments", "--no-names",
            f"--colors={colors}", f"--lossy={lossy}", "--resize-method=sample",
            f"--resize-fit={width}x{height}", "--output", str(target), str(source),
        ]
        try:
            with diagnostics.open("wb") as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise GifCompressionError("gif_compression_timeout") from exc
        except FileNotFoundError as exc:
            raise GifCompressionError("gif_compression_tool_missing") from exc
        if returncode != 0:
            detail = diagnostics.read_text(errors="replace")[-2000:].replace(str(directory), "<gif>")
            logger.error("gif_compression attempt={} exit_code={} diagnostic={}", attempt, returncode, detail)
            code = "gif_processing_memory_exceeded" if "memory" in detail.lower() else "gif_compression_encoder_failed"
            if returncode == -signal.SIGXFSZ:
                code = "gif_compression_output_limit_exceeded"
            raise GifCompressionError(code)
        size = target.stat().st_size
        logger.info("gif_compression attempt={} input_bytes={} output_bytes={} colors={} lossy={} width={} height={}", attempt, len(content), size, colors, lossy, width, height)
        if size > output_limit:
            continue
        compressed = target.read_bytes()
        with Image.open(BytesIO(compressed)) as image:
            final = scan_gif(image)
        if final.durations != original.durations or final.loop != original.loop:
            validation_error = "gif_compression_animation_changed"
            continue
        if original.transparent_frames != final.transparent_frames:
            validation_error = "gif_compression_transparency_changed"
            continue
        return compressed
    raise GifCompressionError(validation_error or "gif_compression_target_exceeded")
