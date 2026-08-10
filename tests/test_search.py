"""无资源包检索索引的稳定性和缓存替换测试。"""

from __future__ import annotations

from pathlib import Path

from backend.config import Settings
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
        vlm_api_key=None,
        vlm_base_url=None,
        vlm_model="vlm",
        vlm_max_attempts=2,
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
    (settings.image_root / "one.png").write_bytes(b"x")
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
