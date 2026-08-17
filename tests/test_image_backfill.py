"""图片处理迁移工具的纯单元契约测试。"""

from __future__ import annotations

from pathlib import Path

from backend.config import Settings
from scripts.backfill_image_processing import _processing_config, _parser


def test_backfill_parser_limits_are_explicit() -> None:
    """迁移命令默认使用有界页大小并提供显式 seed/switch 开关。"""
    args = _parser().parse_args([])
    assert args.page_size == 100
    assert args.seed_only is False
    assert args.switch is False


def test_backfill_config_uses_server_model_identity(tmp_path: Path) -> None:
    """job 配置指纹来自服务端设置，不从图片或客户端字段读取授权信息。"""
    settings = Settings(_env_file=None, data_root=tmp_path, image_root=tmp_path / "images", database_url="postgresql+psycopg://example/example")
    config = _processing_config(settings)
    assert config["embedding_model"] == settings.embedding_model
    assert config["embedding_dimensions"] == 1024
    assert "grant" not in config
    assert "scope_id" not in config
