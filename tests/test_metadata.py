"""图片 sidecar 元数据服务的 schema、指纹和检索文本测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.metadata import MetadataError, MetadataService


def make_image(tmp_path: Path, name: str = "cat.png", content: bytes = b"image") -> tuple[Path, MetadataService]:
    """创建隔离图片根目录和测试图片。"""
    root = tmp_path / "images"
    root.mkdir(parents=True, exist_ok=True)
    image = root / name
    image.write_bytes(content)
    return image, MetadataService(root)


def test_sidecar_preserves_schema_and_unknown_fields(tmp_path: Path):
    """sidecar 使用完整文件名命名，并保留未知扩展字段。"""
    image, service = make_image(tmp_path)
    metadata = service.create_pending(image)
    payload = metadata.model_dump(mode="json")
    payload["custom"] = {"owner": "test"}
    payload["meme_context"]["summary"] = "一只猫正在做出反应"
    service.write(image, payload)
    loaded = service.load(image)
    assert service.sidecar_path(image).name == "cat.png.json"
    assert loaded.model_extra["custom"] == {"owner": "test"}
    assert loaded.meme_context.summary == "一只猫正在做出反应"


def test_same_stem_different_extensions_have_distinct_sidecars(tmp_path: Path):
    """同名不同扩展名的图片不会共享 sidecar。"""
    first, service = make_image(tmp_path, "cat.png", b"png")
    second = first.with_name("cat.gif")
    second.write_bytes(b"gif")
    service.create_pending(first)
    service.create_pending(second)
    assert service.sidecar_path(first) != service.sidecar_path(second)
    assert service.sidecar_path(first).is_file()
    assert service.sidecar_path(second).is_file()


def test_image_change_marks_metadata_repair_required(tmp_path: Path):
    """图片内容变化后，旧 sidecar 不能继续作为有效元数据。"""
    image, service = make_image(tmp_path)
    service.create_pending(image)
    image.write_bytes(b"changed")
    assert service.status(image)["status"] == "repair_required"
    with pytest.raises(MetadataError) as error:
        service.load(image)
    assert error.value.code == "metadata_image_mismatch"


def test_human_fields_are_not_overwritten_by_vision(tmp_path: Path):
    """自动视觉更新不得覆盖人工确认的字段。"""
    image, service = make_image(tmp_path)
    service.create_pending(image)
    service.update_context(image, {"summary": "人工确认摘要"}, producer="human", status="ready")
    service.apply_visual_candidates(image, ["模型猜测摘要", "关键词"])
    loaded = service.load(image)
    assert loaded.meme_context.summary == "人工确认摘要"
    assert loaded.meme_context.keywords == ["模型猜测摘要", "关键词"]


def test_semantic_document_excludes_research_helpers(tmp_path: Path):
    """embedding 文本只包含允许的语义字段。"""
    image, service = make_image(tmp_path)
    service.create_pending(image)
    service.update_context(
        image,
        {
            "summary": "一个人无奈地摊手",
            "subjects": ["人物"],
            "visible_text": ["原文"],
            "references": ["已确认模板"],
            "meaning": "表示无奈",
            "keywords": ["无奈"],
            "search_queries": ["不应进入向量"],
            "uncertainties": ["不确定出处"],
            "source_urls": ["https://example.com/source"],
        },
        producer="research",
        status="ready",
    )
    text = service.embedding_record(image)["text"]
    assert "一个人无奈地摊手" in text
    assert "已确认模板" in text
    assert "不应进入向量" not in text
    assert "不确定出处" not in text
    assert "example.com" not in text


def test_repair_creates_pending_sidecar_and_reports_orphan(tmp_path: Path):
    """修复任务补齐缺失 sidecar，并报告而不删除孤立 JSON。"""
    image, service = make_image(tmp_path)
    nested = image.parent / "nested" / "other.gif"
    nested.parent.mkdir()
    nested.write_bytes(b"other")
    orphan = image.with_name("orphan.png.json")
    orphan.write_text(json.dumps({"orphan": True}), encoding="utf-8")
    result = service.repair()
    assert result["created"] + result["repaired"] == 2
    assert service.sidecar_path(image).is_file()
    assert service.sidecar_path(nested).is_file()
    assert result["orphaned"] == 1
    assert orphan.is_file()
    second = service.repair()
    assert second["processed"] == 2
    assert second["repaired"] == 0
