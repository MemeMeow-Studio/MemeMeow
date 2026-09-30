"""图片资源预检边界。

该模块位于上传、合集导入和后续图片处理入口之前，负责把不可信图片字节
转换为受限的实际格式、帧数和像素事实。解码在独立进程中执行，调用方只会
收到稳定的 ``ImagePreflightError.code``，不会把 Pillow 异常或内部路径返回给客户端。
"""

from __future__ import annotations

import multiprocessing
import __main__
import time
from io import BytesIO
from pathlib import Path
import os
import signal
from typing import Any

from PIL import Image

from backend.gif_compression import (
    GIF_PROCESSING_TIMEOUT_SECONDS, MAX_GIF_UPLOAD_BYTES, GifCompressionError, compress_gif,
    gif_preflight_resources, limit_gif_worker, scan_gif,
)


MAX_IMAGE_FRAME_PIXELS = 25_000_000
MAX_IMAGE_FRAMES = 100
MAX_IMAGE_TOTAL_FRAME_PIXELS = 100_000_000
IMAGE_PREFLIGHT_TIMEOUT_SECONDS = 10.0


class ImagePreflightError(ValueError):
    """图片资源预检失败，``code`` 是可公开的稳定错误码。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ImageUploadPreparation(str):
    """上传预检结果；content 是用于最终 SHA、大小和存储的图片字节。"""

    def __new__(cls, extension: str, content: bytes, actual_format: str, converted: bool):
        """创建携带最终内容及实际格式的扩展名字符串，兼容注入式校验入口。"""
        instance = str.__new__(cls, extension)
        instance.extension = extension
        instance.content = content
        instance.actual_format = actual_format
        instance.converted = converted
        return instance


def _scan_image_content(content: bytes, extension: str) -> None:
    """在当前进程完整扫描图片格式、帧数、像素和可解码性。"""
    expected_formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF"}
    expected_format = expected_formats.get(str(extension).lower())
    if expected_format is None:
        raise ImagePreflightError("unsupported_format")
    if not isinstance(content, bytes):
        raise ImagePreflightError("invalid_image")

    try:
        # verify() 负责检查容器结构；随后重新打开并逐帧 load，避免只验证
        # 动画的首帧而把后续损坏数据交给视觉服务。
        with Image.open(BytesIO(content)) as image:
            if image.format != expected_format:
                raise ImagePreflightError("invalid_image")
            if image.format == "GIF":
                scan_gif(image)
                return
            image.verify()

        with Image.open(BytesIO(content)) as image:
            if image.format != expected_format:
                raise ImagePreflightError("invalid_image")
            try:
                frame_count = int(getattr(image, "n_frames", 1))
            except (AttributeError, TypeError, ValueError):
                frame_count = 1
            if frame_count < 1:
                raise ImagePreflightError("invalid_image")
            if frame_count > MAX_IMAGE_FRAMES:
                raise ImagePreflightError("image_frame_count_exceeded")

            total_pixels = 0
            for frame_index in range(frame_count):
                if frame_index:
                    image.seek(frame_index)
                width, height = image.size
                if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
                    raise ImagePreflightError("invalid_image")
                frame_pixels = width * height
                if frame_pixels > MAX_IMAGE_FRAME_PIXELS:
                    raise ImagePreflightError("image_frame_pixels_exceeded")
                total_pixels += frame_pixels
                if total_pixels > MAX_IMAGE_TOTAL_FRAME_PIXELS:
                    raise ImagePreflightError("image_total_pixels_exceeded")
                image.load()
    except GifCompressionError as exc:
        raise ImagePreflightError(exc.code) from exc
    except ImagePreflightError:
        raise
    except MemoryError as exc:
        raise ImagePreflightError("gif_processing_memory_exceeded" if expected_format == "GIF" else "image_preflight_failed") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImagePreflightError("invalid_image") from exc


def _image_preflight_worker(source_path: str, extension: str, connection: Any, normalize_extension: bool = False, directory: str = "", is_gif: bool = False) -> None:
    """子进程入口，只通过管道返回稳定结果并始终关闭管道。"""
    try:
        if is_gif:
            limit_gif_worker()
        content = Path(source_path).read_bytes()
        _scan_image_content(content, extension)
        if normalize_extension:
            final_content = compress_gif(content, Path(directory)) if is_gif else content
            connection.send({"content": final_content, "extension": extension, "actual_format": "GIF" if is_gif else "PNG" if extension == ".png" else "JPEG", "converted": final_content != content})
        else:
            connection.send(None)
    except (ImagePreflightError, GifCompressionError) as exc:
        try:
            connection.send(exc.code)
        except (BrokenPipeError, EOFError, OSError):
            pass
    except MemoryError:
        connection.send("gif_processing_memory_exceeded" if is_gif else "image_preflight_failed")
    except BaseException:  # noqa: BLE001
        try:
            connection.send("image_preflight_failed")
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            connection.close()
        except OSError:
            pass


def _terminate_process(process: Any) -> None:
    """终止并回收超时或异常的预检进程，避免后台解码继续运行。"""
    try:
        # GIF worker 独立进程组的 ID 保持为 worker PID，worker 退出后仍可回收编码器。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.is_alive():
            process.terminate()
    finally:
        process.join(timeout=1.0)
        if process.is_alive() and callable(getattr(process, "kill", None)):
            process.kill()
            process.join(timeout=1.0)


def _preflight_process_context() -> Any:
    """选择不会递归启动应用的进程上下文，并兼容无主脚本的交互入口。"""
    main_file = getattr(__main__, "__file__", None)
    if isinstance(main_file, str) and Path(main_file).is_file():
        return multiprocessing.get_context("spawn")
    methods = multiprocessing.get_all_start_methods()
    if "fork" in methods:
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context("spawn")


def _run_preflight(content: bytes, extension: str, *, timeout_seconds: float | None, normalize_extension: bool) -> ImageUploadPreparation | None:
    """在受限子进程中预检一张图片。

    输入是图片字节、后缀和执行模式；普通上传返回最终字节，严格验证返回 None。
    GIF 通过独立内存及时间预算验证，并在普通上传时执行大小限制和压缩。
    """
    if str(extension).lower() not in {".png", ".jpg", ".jpeg", ".gif"}:
        raise ImagePreflightError("unsupported_format")
    if not isinstance(content, (bytes, bytearray)):
        raise ImagePreflightError("invalid_image")
    is_gif = content.startswith((b"GIF87a", b"GIF89a"))
    if is_gif and len(content) > MAX_GIF_UPLOAD_BYTES:
        raise ImagePreflightError("file_too_large")
    try:
        timeout = float(timeout_seconds if timeout_seconds is not None else GIF_PROCESSING_TIMEOUT_SECONDS if is_gif else IMAGE_PREFLIGHT_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as exc:
        raise ImagePreflightError("image_preflight_failed") from exc
    if timeout <= 0:
        raise ImagePreflightError("image_preflight_failed")

    try:
        with gif_preflight_resources(is_gif) as directory:
            return _execute_preflight(content, extension, normalize_extension=normalize_extension, timeout=timeout, directory=directory)
    except GifCompressionError as exc:
        raise ImagePreflightError(exc.code) from exc


def _execute_preflight(content: bytes, extension: str, *, normalize_extension: bool, timeout: float, directory: Path) -> ImageUploadPreparation | None:
    """在父进程拥有的临时目录内启动 worker，按截止时间接收结果并回收进程。"""
    is_gif = content.startswith((b"GIF87a", b"GIF89a"))
    deadline = time.monotonic() + timeout
    source = directory / "input.image"
    source.write_bytes(content)
    process_context = _preflight_process_context()
    parent, child = process_context.Pipe(duplex=False)
    process = process_context.Process(target=_image_preflight_worker, args=(str(source), str(extension), child, normalize_extension, str(directory), is_gif), daemon=True)
    try:
        process.start()
    except (AssertionError, OSError, RuntimeError) as exc:
        child.close()
        parent.close()
        raise ImagePreflightError("image_preflight_failed") from exc
    child.close()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise ImagePreflightError("gif_compression_timeout" if is_gif else "image_preflight_timeout")
            try:
                ready = parent.poll(min(remaining, 0.05))
            except (EOFError, OSError) as exc:
                raise ImagePreflightError("image_preflight_failed") from exc
            if ready:
                break
            if not process.is_alive():
                raise ImagePreflightError("image_preflight_failed")
        try:
            result = parent.recv()
        except (EOFError, OSError) as exc:
            raise ImagePreflightError("image_preflight_failed") from exc
        if isinstance(result, str):
            raise ImagePreflightError(result)
        if normalize_extension:
            return ImageUploadPreparation(**result)
        return None
    finally:
        # 收到结果后也要 join；若子进程仍在退出，不能遗留孤儿解码进程。
        process.join(timeout=1.0)
        _terminate_process(process)
        parent.close()


def validate_image_content(content: bytes, extension: str, *, timeout_seconds: float | None = None) -> None:
    """在受限进程内验证已有图片和合集成员；保持原始内容及指纹。"""
    _run_preflight(content, extension, timeout_seconds=timeout_seconds, normalize_extension=False)


def preflight_upload_image(content: bytes, extension: str, *, timeout_seconds: float | None = None) -> ImageUploadPreparation:
    """普通上传的最终内容预检；GIF 超过 10MB 时压缩，成功后返回存储字节。"""
    return _run_preflight(content, extension, timeout_seconds=timeout_seconds, normalize_extension=True)
