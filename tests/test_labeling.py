"""VLM 描述有限重试行为测试。"""

from types import SimpleNamespace

from backend.config import Settings
from backend.labeling import LabelingService


def test_describe_retries_then_returns_candidates(tmp_path, monkeypatch):
    """首次模型失败后只进行有限重试，并解析成功响应。"""
    monkeypatch.setenv("MEMEMEOW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("MEMEMEOW_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("VLM_API_KEY", "key")
    monkeypatch.setenv("VLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("VLM_MAX_ATTEMPTS", "2")
    settings = Settings.from_env(tmp_path / "missing.env")
    image = tmp_path / "image.png"
    image.write_bytes(b"content")
    calls = {"count": 0}

    class Completions:
        def create(self, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary")
            message = SimpleNamespace(content="候选一\n候选二")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr("backend.labeling.OpenAI", lambda **kwargs: fake_client)
    assert LabelingService(settings).describe(image) == ["候选一", "候选二"]
    assert calls["count"] == 2
