# 尺寸输出编码进程：只加载图片处理依赖，保持 GIF 的进程内存预算。

from __future__ import annotations

import json
import subprocess
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import Image, ImageOps

from backend.gif_compression import GifCompressionError, limit_gif_worker, scan_gif


def image_export_worker(
    content: bytes, extension: str, max_edge: int, output_limit: int, temp_limit: int,
    connection: Any, *, directory: Path,
) -> None:
    """在受限进程中缩小图片，通过 Pipe 返回输出字节、尺寸或明确错误码。"""
    try:
        expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF"}.get(extension.lower())
        if expected is None:
            raise GifCompressionError("unsupported_format")
        if expected == "GIF":
            limit_gif_worker()
        with Image.open(BytesIO(content)) as image:
            if image.format != expected:
                raise GifCompressionError("invalid_image")
            width, height = image.size
            if width < 1 or height < 1 or width * height * 8 > temp_limit:
                raise GifCompressionError("image_export_temp_limit_exceeded")
            if max(width, height) <= max_edge:
                result = content
            elif expected == "GIF":
                original = scan_gif(image)
                target = directory / "resized.gif"
                diagnostics = directory / "gifsicle.log"
                command = [
                    "gifsicle", "--no-ignore-errors", "--conserve-memory", "--threads=1",
                    "--optimize=keep-empty", "--resize-method=sample", f"--resize-fit={max_edge}x{max_edge}",
                ]
                with target.open("wb") as output, diagnostics.open("wb") as errors:
                    try:
                        completed = subprocess.run(command, input=content, stdout=output, stderr=errors, check=False)
                    except FileNotFoundError as exc:
                        raise GifCompressionError("image_export_tool_missing") from exc
                if completed.returncode != 0:
                    detail = diagnostics.read_text(errors="replace")[-2000:].replace(str(directory), "<image-export>")
                    logger.error("image_export encoder_exit={} diagnostic={}", completed.returncode, detail)
                    raise GifCompressionError("image_export_encoder_failed")
                if target.stat().st_size > output_limit:
                    raise GifCompressionError("image_export_output_limit_exceeded")
                result = target.read_bytes()
                with Image.open(BytesIO(result)) as resized:
                    final = scan_gif(resized)
                    width, height = final.size
                if original.durations != final.durations or original.loop != final.loop:
                    raise GifCompressionError("image_export_animation_changed")
                if original.transparent_frames != final.transparent_frames:
                    raise GifCompressionError("image_export_transparency_changed")
            else:
                image.load()
                converted = ImageOps.exif_transpose(image)
                converted.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                if expected == "JPEG" and converted.mode not in {"RGB", "L"}:
                    converted = converted.convert("RGB")
                if expected == "PNG" and converted.mode in {"P", "1"}:
                    converted = converted.convert("RGBA")
                output = BytesIO()
                options = {"quality": 95} if expected == "JPEG" else {}
                converted.save(output, format=expected, **options)
                result = output.getvalue()
                width, height = converted.size
        if not result or len(result) > output_limit:
            raise GifCompressionError("image_export_output_limit_exceeded")
        connection.send_bytes(b"M" + json.dumps({"width": width, "height": height, "size": len(result)}).encode("ascii"))
        for offset in range(0, len(result), 64 * 1024):
            connection.send_bytes(b"D" + result[offset : offset + 64 * 1024])
        connection.send_bytes(b"E")
    except GifCompressionError as exc:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send_bytes(b"X" + exc.code.encode("ascii"))
    except MemoryError:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send_bytes(b"Ximage_export_memory_exceeded")
    except Exception as exc:
        logger.exception("image_export stage=encode error_type={}", type(exc).__name__)
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send_bytes(b"Ximage_export_encode_failed")
    finally:
        connection.close()
