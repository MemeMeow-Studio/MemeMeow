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
from typing import Any

from PIL import Image


MAX_IMAGE_FRAME_PIXELS = 25_000_000
MAX_IMAGE_FRAMES = 100
MAX_IMAGE_TOTAL_FRAME_PIXELS = 100_000_000
IMAGE_PREFLIGHT_TIMEOUT_SECONDS = 10.0


class ImagePreflightError(ValueError):
    """图片资源预检失败，``code`` 是可公开的稳定错误码。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
    except ImagePreflightError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImagePreflightError("invalid_image") from exc


def _image_preflight_worker(content: bytes, extension: str, connection: Any) -> None:
    """子进程入口，只通过管道返回稳定结果并始终关闭管道。"""
    try:
        _scan_image_content(content, extension)
        connection.send(None)
    except ImagePreflightError as exc:
        try:
            connection.send(exc.code)
        except (BrokenPipeError, EOFError, OSError):
            pass
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


def validate_image_content(content: bytes, extension: str, *, timeout_seconds: float = IMAGE_PREFLIGHT_TIMEOUT_SECONDS) -> None:
    """在受限子进程中预检一张图片。

    输入是尚未入库的图片字节和客户端文件扩展名；成功时返回 ``None``，失败时
    抛出稳定的 ``ImagePreflightError``。调用场景是直接上传和合集 ZIP 成员写入前。
    """
    if str(extension).lower() not in {".png", ".jpg", ".jpeg", ".gif"}:
        raise ImagePreflightError("unsupported_format")
    if not isinstance(content, (bytes, bytearray)):
        raise ImagePreflightError("invalid_image")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ImagePreflightError("image_preflight_failed") from exc
    if timeout <= 0:
        raise ImagePreflightError("image_preflight_failed")

    process_context = _preflight_process_context()
    parent, child = process_context.Pipe(duplex=False)
    process = process_context.Process(target=_image_preflight_worker, args=(bytes(content), str(extension), child), daemon=True)
    try:
        process.start()
    except (AssertionError, OSError, RuntimeError) as exc:
        child.close()
        parent.close()
        raise ImagePreflightError("image_preflight_failed") from exc
    child.close()
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise ImagePreflightError("image_preflight_timeout")
            try:
                ready = parent.poll(min(remaining, 0.05))
            except (EOFError, OSError) as exc:
                raise ImagePreflightError("image_preflight_failed") from exc
            if ready:
                break
            if not process.is_alive():
                raise ImagePreflightError("image_preflight_failed")
        try:
            error_code = parent.recv()
        except (EOFError, OSError) as exc:
            raise ImagePreflightError("image_preflight_failed") from exc
        if error_code:
            raise ImagePreflightError(str(error_code))
    finally:
        # 收到结果后也要 join；若子进程仍在退出，不能遗留孤儿解码进程。
        process.join(timeout=1.0)
        if process.is_alive():
            _terminate_process(process)
        parent.close()
