"""Pydantic Settings 与受保护 dotenv 持久化测试。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import Settings, update_dotenv_concurrency
from backend.visual import VISUAL_DIMENSIONS, VISUAL_MODEL_ID, VISUAL_PREPROCESS_VERSION
from backend.visual_models import visual_model_spec


def test_visual_defaults_pin_dinov2_vitb14_identity() -> None:
    """默认配置必须固定活动 DINOv2 ViT-B/14 视觉空间。"""
    settings = Settings(_env_file=None)
    assert settings.visual_model == VISUAL_MODEL_ID == "dinov2_vitb14"
    assert settings.visual_model_dimensions == VISUAL_DIMENSIONS == 768
    assert settings.visual_preprocess_version == VISUAL_PREPROCESS_VERSION


def test_visual_known_previous_model_requires_schema_migration() -> None:
    """已归档的 DINOv3 模型不能仅靠环境变量重新启用。"""
    with pytest.raises(ValidationError, match="visual_model_migration_required"):
        Settings(_env_file=None, visual_model="dinov3_vith16plus", visual_model_dimensions=1280, visual_preprocess_version="dinov3_vith16plus-rgb224-first-frame-v1")


def test_visual_registry_preserves_dinov3_history_without_enabling_mixed_space() -> None:
    """模型清单继续保留 DINOv3 身份，但活动运行时不会加载其向量空间。"""
    historical = visual_model_spec("dinov3_vith16plus")
    assert historical is not None
    assert historical.dimensions == 1280
    assert historical.source_commit == "6876159a11b4df116f30f667f8c9888617df0751"
    assert historical.runtime_supported is False


def test_visual_known_model_rejects_dimension_override() -> None:
    """当前模型维度不能被配置覆盖成另一向量空间。"""
    with pytest.raises(ValidationError, match="visual_dimensions_must_be_768"):
        Settings(_env_file=None, visual_model="dinov2_vitb14", visual_model_dimensions=1280)


def test_settings_preserve_environment_priority_and_unknown_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """进程环境覆盖 dotenv，未知变量不会阻断启动。"""
    env_file = tmp_path / ".env"
    env_file.write_text("MEMEMEOW_OPENCODE_CONCURRENCY=2\nUNKNOWN_VALUE=ignored\n", encoding="utf-8")
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", "3")
    settings = Settings.from_env(env_file)
    assert settings.opencode_concurrency == 3
    assert "UNKNOWN_VALUE" not in repr(settings)


@pytest.mark.parametrize("value", ["0", "9", "not-an-int"])
def test_settings_reject_invalid_concurrency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str):
    """并发配置必须在 1..8 范围内且为整数。"""
    monkeypatch.setenv("MEMEMEOW_OPENCODE_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        Settings.from_env(tmp_path / ".missing-env")


def test_dotenv_update_is_atomic_preserves_comments_and_uses_safe_mode(tmp_path: Path):
    """并发字段更新保留未知内容、注释，并将文件权限限制到用户读写。"""
    target = tmp_path / ".env"
    target.write_text("# keep\nOTHER=value\nMEMEMEOW_OPENCODE_CONCURRENCY=1\n", encoding="utf-8")
    update_dotenv_concurrency(target, 4)
    assert target.read_text(encoding="utf-8") == "# keep\nOTHER=value\nMEMEMEOW_OPENCODE_CONCURRENCY=4\n"
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0


def test_dotenv_update_accepts_spacing_and_restores_owner_write_permission(tmp_path: Path):
    """更新带等号空格的字段，并确保只读旧文件恢复为用户可写权限。"""
    target = tmp_path / ".env"
    target.write_text("MEMEMEOW_OPENCODE_CONCURRENCY = 1 # keep\n", encoding="utf-8")
    target.chmod(0o400)
    update_dotenv_concurrency(target, 2)
    assert target.read_text(encoding="utf-8") == "MEMEMEOW_OPENCODE_CONCURRENCY=2 # keep\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_dotenv_update_rejects_non_integer_value(tmp_path: Path):
    """配置写入工具不接受浮点数或布尔值的隐式截断。"""
    with pytest.raises(ValueError):
        update_dotenv_concurrency(tmp_path / ".env", 1.5)
    with pytest.raises(ValueError):
        update_dotenv_concurrency(tmp_path / ".env", True)


def test_dotenv_symlink_is_rejected(tmp_path: Path):
    """设置写入不能跟随符号链接越过受控配置文件。"""
    target = tmp_path / ".env"
    target.write_text("OTHER=value\n", encoding="utf-8")
    link = tmp_path / "link.env"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        update_dotenv_concurrency(link, 2)
