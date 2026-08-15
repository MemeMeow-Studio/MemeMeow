"""视觉模型清单与持久化迁移边界。

该模块位于视觉服务和主后端配置之间，集中声明官方模型的维度、源码提交、
checkpoint 文件名以及预处理身份。当前活动空间固定为 DINOv2 ViT-B/14；
历史 DINOv3 空间仍保留清单，只有在对应的独立 Alembic 迁移完成后才能启用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DINOV2_SOURCE_COMMIT = "7764ea0f912e53c92e82eb78a2a1631e92725fc8"
DINOV3_SOURCE_COMMIT = "6876159a11b4df116f30f667f8c9888617df0751"
ACTIVE_VISUAL_MODEL_ID = "dinov2_vitb14"
ACTIVE_VISUAL_SCHEMA_REVISION = "0009_dinov2_vitb14_visual_search"


@dataclass(frozen=True)
class VisualModelSpec:
    """一个官方视觉模型空间的静态身份和部署来源。"""

    model: str
    dimensions: int
    preprocess_version: str
    source_commit: str
    source_package: str
    source_files: tuple[str, ...]
    backbone_name: str
    checkpoint_filename: str
    checkpoint_url: str
    schema_revision: str
    runtime_supported: bool = False

    @property
    def source_marker_filename(self) -> str:
        """返回源码目录中用于防止版本漂移的提交标记文件名。"""
        return f".mememeow-{self.source_package}-source-commit"


_DINOV2_SOURCE_FILES = (
    "dinov2/__init__.py",
    "dinov2/hub/backbones.py",
    "dinov2/models/vision_transformer.py",
)
_DINOV3_SOURCE_FILES = (
    "dinov3/__init__.py",
    "dinov3/hub/backbones.py",
    "dinov3/models/vision_transformer.py",
)


VISUAL_MODEL_SPECS: dict[str, VisualModelSpec] = {
    "dinov2_vits14": VisualModelSpec(
        model="dinov2_vits14",
        dimensions=384,
        preprocess_version="dinov2_vits14-rgb224-first-frame-v1",
        source_commit=DINOV2_SOURCE_COMMIT,
        source_package="dinov2",
        source_files=_DINOV2_SOURCE_FILES,
        backbone_name="dinov2_vits14",
        checkpoint_filename="dinov2_vits14_pretrain.pth",
        checkpoint_url="https://dl.fbaipublicfiles.com/dinov2/dinov2_vits14/dinov2_vits14_pretrain.pth",
        schema_revision="future-dinov2-vits14",
    ),
    "dinov2_vitb14": VisualModelSpec(
        model="dinov2_vitb14",
        dimensions=768,
        preprocess_version="dinov2_vitb14-rgb224-first-frame-v1",
        source_commit=DINOV2_SOURCE_COMMIT,
        source_package="dinov2",
        source_files=_DINOV2_SOURCE_FILES,
        backbone_name="dinov2_vitb14",
        checkpoint_filename="dinov2_vitb14_pretrain.pth",
        checkpoint_url="https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/dinov2_vitb14_pretrain.pth",
        schema_revision=ACTIVE_VISUAL_SCHEMA_REVISION,
        runtime_supported=True,
    ),
    "dinov2_vitl14": VisualModelSpec(
        model="dinov2_vitl14",
        dimensions=1024,
        preprocess_version="dinov2_vitl14-rgb224-first-frame-v1",
        source_commit=DINOV2_SOURCE_COMMIT,
        source_package="dinov2",
        source_files=_DINOV2_SOURCE_FILES,
        backbone_name="dinov2_vitl14",
        checkpoint_filename="dinov2_vitl14_pretrain.pth",
        checkpoint_url="https://dl.fbaipublicfiles.com/dinov2/dinov2_vitl14/dinov2_vitl14_pretrain.pth",
        schema_revision="future-dinov2-vitl14",
    ),
    "dinov2_vitg14": VisualModelSpec(
        model="dinov2_vitg14",
        dimensions=1536,
        preprocess_version="dinov2_vitg14-rgb224-first-frame-v1",
        source_commit=DINOV2_SOURCE_COMMIT,
        source_package="dinov2",
        source_files=_DINOV2_SOURCE_FILES,
        backbone_name="dinov2_vitg14",
        checkpoint_filename="dinov2_vitg14_pretrain.pth",
        checkpoint_url="https://dl.fbaipublicfiles.com/dinov2/dinov2_vitg14/dinov2_vitg14_pretrain.pth",
        schema_revision="future-dinov2-vitg14",
    ),
    "dinov3_vith16plus": VisualModelSpec(
        model="dinov3_vith16plus",
        dimensions=1280,
        preprocess_version="dinov3_vith16plus-rgb224-first-frame-v1",
        source_commit=DINOV3_SOURCE_COMMIT,
        source_package="dinov3",
        source_files=_DINOV3_SOURCE_FILES,
        backbone_name="dinov3_vith16plus",
        checkpoint_filename="dinov3_vith16plus_pretrain_lvd1689m-7c1da9a5.pth",
        checkpoint_url="https://dl.fbaipublicfiles.com/dinov3/dinov3_vith16plus/dinov3_vith16plus_pretrain_lvd1689m-7c1da9a5.pth",
        schema_revision="0008_dinov3_vith16plus",
        runtime_supported=False,
    ),
}


def visual_model_spec(model: str) -> VisualModelSpec | None:
    """按模型标识返回清单项；未知标识交给测试夹具或未来扩展处理。"""
    return VISUAL_MODEL_SPECS.get(str(model).strip())


def active_visual_model_spec() -> VisualModelSpec:
    """返回当前发布版本唯一允许加载的模型清单项。"""
    return VISUAL_MODEL_SPECS[ACTIVE_VISUAL_MODEL_ID]


def source_repository_valid(source: str | Path | None, model: str) -> bool:
    """检查指定模型的官方源码最小文件集合和固定提交标记。"""
    spec = visual_model_spec(model)
    if spec is None or source is None:
        return False
    try:
        root = Path(source).expanduser()
        if not root.is_dir() or not all((root / relative).is_file() for relative in spec.source_files):
            return False
        marker = root / spec.source_marker_filename
        return marker.is_file() and marker.read_text(encoding="ascii").strip() == spec.source_commit
    except (OSError, TypeError, ValueError, UnicodeError):
        return False
