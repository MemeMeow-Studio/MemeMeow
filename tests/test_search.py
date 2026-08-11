"""无资源包检索索引的稳定性和缓存替换测试。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.config import Settings
from backend.metadata import MetadataService
from backend.search import SearchService


def make_settings(tmp_path: Path) -> Settings:
    """构造隔离的检索配置。"""
    return Settings(
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        embedding_api_key="key",
        embedding_base_url="https://example.invalid/v1",
        embedding_model="model",
        llm_enhance_model=None,
        protected_mode=False,
        allowed_endpoints=("/",),
        rate_limit_enabled=False,
        rate_limit_requests=60,
        rate_limit_window=60,
        max_upload_size=1024,
    )


def test_search_sorting_deduplication_and_missing_files(tmp_path: Path, monkeypatch):
    """结果按分数和相对路径稳定排序，并跳过缺失图片。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    (settings.image_root / "b.png").write_bytes(b"x")
    (settings.image_root / "a.png").write_bytes(b"x")
    service = SearchService(settings)
    service._items = [
        {"path": "b.png", "embedding": [1.0, 0.0], "label": "b"},
        {"path": "a.png", "embedding": [1.0, 0.0], "label": "a"},
        {"path": "a.png", "embedding": [1.0, 0.0], "label": "duplicate"},
        {"path": "missing.png", "embedding": [1.0, 0.0], "label": "missing"},
    ]
    monkeypatch.setattr(service, "_embedding", lambda text: [1.0, 0.0])
    assert service.search("query", 5) == [str((settings.image_root / "a.png").resolve()), str((settings.image_root / "b.png").resolve())]


def test_cache_generation_replaces_only_after_success(tmp_path: Path, monkeypatch):
    """缓存生成失败时旧缓存仍可继续使用，成功后一次性替换。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    image = settings.image_root / "one.png"
    image.write_bytes(b"x")
    metadata = MetadataService(settings.image_root)
    metadata.create_pending(image)
    metadata.update_context(image, {"summary": "可索引图片"}, producer="research", status="ready")
    service = SearchService(settings)
    service._items = [{"path": "old.png", "embedding": [1.0], "label": "old"}]
    calls = {"count": 0}

    def embedding(text):
        calls["count"] += 1
        raise RuntimeError("model failed")

    monkeypatch.setattr(service, "_embedding", embedding)
    try:
        service.generate_cache(lambda *_: None)
    except RuntimeError:
        pass
    assert service._items[0]["path"] == "old.png"

    monkeypatch.setattr(service, "_embedding", lambda text: [1.0])
    service.generate_cache(lambda *_: None)
    assert service._items[0]["path"] == "one.png"
    assert calls["count"] == 1


def test_cache_generation_uses_meme_context_whitelist(tmp_path: Path, monkeypatch):
    """v4 缓存使用语境白名单，并保存图片与 sidecar 指纹。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    image = settings.image_root / "cat.png"
    image.write_bytes(b"image")
    metadata = MetadataService(settings.image_root)
    metadata.create_pending(image)
    metadata.update_context(
        image,
        {
            "title": "猫猫无奈摊手",
            "summary": "一只猫无奈地摊手",
            "subjects": ["猫"],
            "visible_text": ["为什么"],
            "references": ["确认的模板"],
            "meaning": "表达无奈",
            "keywords": ["无奈"],
            "search_queries": ["不得进入索引"],
            "uncertainties": ["不确定出处"],
            "source_urls": ["https://example.com/source"],
        },
        producer="research",
        status="ready",
    )
    captured: list[str] = []
    service = SearchService(settings, metadata)
    monkeypatch.setattr(service, "_embedding", lambda text: captured.append(text) or [1.0])
    service.generate_cache(lambda *_: None)
    assert captured == ["标题：猫猫无奈摊手\n摘要：一只猫无奈地摊手\n主体：猫\n图片文字：为什么\n已确认引用：确认的模板\n常见含义：表达无奈\n关键词：无奈"]
    payload = json.loads((settings.data_root / "search-cache-v4.json").read_text(encoding="utf-8"))
    assert payload["version"] == 4
    assert payload["items"][0]["metadata_hash"]
    assert "不得进入索引" not in payload["items"][0]["semantic_document"]


def test_cache_is_rejected_after_metadata_change(tmp_path: Path, monkeypatch):
    """sidecar 语义变化后，新服务不会加载旧索引。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    image = settings.image_root / "cat.png"
    image.write_bytes(b"image")
    metadata = MetadataService(settings.image_root)
    metadata.create_pending(image)
    metadata.update_context(image, {"summary": "原始摘要"}, producer="research", status="ready")
    service = SearchService(settings, metadata)
    monkeypatch.setattr(service, "_embedding", lambda text: [1.0])
    service.generate_cache(lambda *_: None)
    metadata.update_context(image, {"summary": "更新后的摘要"}, producer="research", status="ready")
    assert not SearchService(settings, metadata).has_cache()


def test_legacy_v3_cache_is_rejected(tmp_path: Path):
    """旧文件名索引不会被当作 v4 语境索引加载。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    image = settings.image_root / "cat.png"
    image.write_bytes(b"image")
    settings.data_root.mkdir(parents=True)
    (settings.data_root / "search-cache-v4.json").write_text(
        json.dumps({"version": 3, "model": settings.embedding_model, "items": [{"path": "cat.png", "embedding": [1.0]}]}),
        encoding="utf-8",
    )
    assert not SearchService(settings).has_cache()


def test_all_unindexable_images_keep_existing_cache(tmp_path: Path, monkeypatch):
    """全部待研究时不调用 embedding，也不发布空缓存。"""
    settings = make_settings(tmp_path)
    settings.image_root.mkdir(parents=True)
    (settings.image_root / "pending.png").write_bytes(b"image")
    MetadataService(settings.image_root).create_pending(settings.image_root / "pending.png")
    service = SearchService(settings)
    service._items = [{"path": "old.png", "embedding": [1.0]}]
    called = []
    monkeypatch.setattr(service, "_embedding", lambda text: called.append(text) or [1.0])
    try:
        service.generate_cache(lambda *_: None)
    except RuntimeError as exc:
        assert str(exc) == "no_indexable_images"
    else:
        raise AssertionError("应拒绝发布空缓存")
    assert called == []
    assert service._items[0]["path"] == "old.png"
