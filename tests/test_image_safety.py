"""共享图片资源预检的像素、帧数、格式和可终止执行测试。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from backend.image_safety import ImagePreflightError, validate_image_content


def png_bytes(size: tuple[int, int] = (3, 3)) -> bytes:
    """生成指定尺寸的可解码 PNG。"""
    output = io.BytesIO()
    Image.new("RGB", size, color="red").save(output, format="PNG")
    return output.getvalue()


def gif_bytes(frame_size: tuple[int, int], frame_count: int) -> bytes:
    """生成帧尺寸和数量可控的 GIF，尽量复用相同像素降低测试输入大小。"""
    frames = []
    for index in range(frame_count):
        frame = Image.new("P", frame_size, color=0)
        frame.putpixel((0, 0), index % 256)
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=1, optimize=False)
    return output.getvalue()


def test_valid_image_passes_shared_preflight() -> None:
    """合法静态图片可以通过共享预检。"""
    validate_image_content(png_bytes(), ".png")


def test_single_frame_pixel_limit_is_checked_before_decode() -> None:
    """单帧超过 25M 像素时在加载像素前拒绝。"""
    with pytest.raises(ImagePreflightError) as error:
        validate_image_content(png_bytes((5_001, 5_000)), ".png")
    assert error.value.code == "image_frame_pixels_exceeded"


def test_animation_frame_count_and_total_pixels_are_bounded() -> None:
    """动画同时受 100 帧和 100M 累计帧像素边界约束。"""
    with pytest.raises(ImagePreflightError) as frame_error:
        validate_image_content(gif_bytes((1, 1), 101), ".gif")
    assert frame_error.value.code == "image_frame_count_exceeded"

    with pytest.raises(ImagePreflightError) as pixels_error:
        validate_image_content(gif_bytes((3_200, 3_200), 10), ".gif")
    assert pixels_error.value.code == "image_total_pixels_exceeded"


def test_extension_and_actual_format_must_match() -> None:
    """扩展名伪装的实际格式被拒绝。"""
    with pytest.raises(ImagePreflightError) as error:
        validate_image_content(png_bytes(), ".gif")
    assert error.value.code == "invalid_image"


def test_tiny_deadline_terminates_preflight_process() -> None:
    """预检截止时间到达后返回超时而不是留下后台解码。"""
    with pytest.raises(ImagePreflightError) as error:
        validate_image_content(png_bytes(), ".png", timeout_seconds=0.001)
    assert error.value.code == "image_preflight_timeout"
