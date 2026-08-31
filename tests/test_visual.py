"""视觉模型适配器、薄客户端和 CLI 契约测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.visual import (
    VISUAL_DIMENSIONS,
    VISUAL_MODEL_ID,
    VISUAL_PREPROCESS_VERSION,
    VisualEmbeddingError,
    VisualInferenceClient,
    VisualModelRunner,
    validate_embedding,
)


def test_visual_identity_and_unconfigured_health_are_deterministic(tmp_path: Path) -> None:
    """没有合法权重时服务只能返回脱敏的配置错误。"""
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", data_root=tmp_path / "data", image_root=tmp_path / "images")
    health = VisualModelRunner(settings).health()
    assert health == {
        "status": "degraded",
        "available": False,
        "model": VISUAL_MODEL_ID,
        "dimensions": VISUAL_DIMENSIONS,
        "preprocess_version": VISUAL_PREPROCESS_VERSION,
        "error": "visual_model_not_configured",
    }
    assert "weights" not in json.dumps(health, ensure_ascii=False)


@pytest.mark.parametrize(
    ("vector", "code"),
    [([0.0] * VISUAL_DIMENSIONS, "visual_embedding_zero_norm"), ([float("nan")] + [0.0] * (VISUAL_DIMENSIONS - 1), "visual_embedding_non_finite"), ([1.0], "visual_embedding_dimensions_mismatch")],
)
def test_visual_vector_validation_reports_stable_codes(vector: list[float], code: str) -> None:
    """零范数、非 finite 和错误维度都不能落库。"""
    with pytest.raises(VisualEmbeddingError, match=code):
        validate_embedding(vector)


def test_visual_client_rejects_default_endpoint_without_model_configuration(tmp_path: Path) -> None:
    """默认本地端点没有权重配置时返回 model_not_configured，而非伪造成功。"""
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://test:test@localhost/test", data_root=tmp_path / "data", image_root=tmp_path / "images")
    with pytest.raises(VisualEmbeddingError) as captured:
        VisualInferenceClient(settings).embed(b"not-an-image")
    assert captured.value.code == "visual_model_not_configured"


def test_visual_client_health_rejects_mismatched_model_identity(tmp_path: Path) -> None:
    """视觉服务即使自报 available，也不能以错误模型身份被 API 采用。"""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/test",
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        visual_health_url="http://visual:8276/health",
    )

    class Response:
        """提供固定健康 JSON 的最小 HTTP 响应夹具。"""

        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, _size: int = -1) -> bytes:
            return json.dumps(
                {
                    "status": "ok",
                    "available": True,
                    "model": "wrong-model",
                    "dimensions": VISUAL_DIMENSIONS,
                    "preprocess_version": VISUAL_PREPROCESS_VERSION,
                }
            ).encode("utf-8")

    health = VisualInferenceClient(settings, opener=lambda *_args, **_kwargs: Response()).health()
    assert health["available"] is False
    assert health["error"] == "visual_model_identity_mismatch"


def test_visual_runner_requires_fixed_official_source_for_file_weights(tmp_path: Path) -> None:
    """仅提供 checkpoint 而没有固定 DINOv2 源码时不得尝试 TorchScript 或随机模型。"""
    weights = tmp_path / "model.pth"
    weights.write_bytes(b"checkpoint")
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/test",
        data_root=tmp_path / "data",
        image_root=tmp_path / "images",
        visual_weights_path=weights,
    )
    health = VisualModelRunner(settings).health()
    assert health["error"] == "visual_model_source_not_configured"
    assert health["available"] is False


def test_visual_source_marker_is_required_for_fixed_revision(tmp_path: Path) -> None:
    """源码目录缺少固定提交标记时必须拒绝加载，防止架构版本漂移。"""
    source = tmp_path / "dinov2"
    for relative in ("dinov2/__init__.py", "dinov2/hub/backbones.py", "dinov2/models/vision_transformer.py"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# test source\n", encoding="utf-8")
    assert not VisualModelRunner._source_repository_valid(source, VISUAL_MODEL_ID)
    (source / ".mememeow-dinov2-source-commit").write_text("7764ea0f912e53c92e82eb78a2a1631e92725fc8\n", encoding="ascii")
    assert VisualModelRunner._source_repository_valid(source, VISUAL_MODEL_ID)


def test_visual_checkpoint_loader_rejects_non_state_dict_without_unsafe_fallback(tmp_path: Path) -> None:
    """checkpoint 不是 state dict 时返回稳定格式错误，不能退回不安全的 pickle 加载。"""
    class FakeTorch:
        """只实现 checkpoint 读取接口的安全加载夹具。"""

        @staticmethod
        def load(_path: str, *, map_location: str, weights_only: bool) -> object:
            """返回非法顶层对象并记录调用约束。"""
            assert map_location == "cpu"
            assert weights_only is True
            return ["not-a-state-dict"]

    with pytest.raises(VisualEmbeddingError) as captured:
        VisualModelRunner._load_checkpoint(FakeTorch(), tmp_path / "model.pth")
    assert captured.value.code == "visual_checkpoint_format_invalid"


def test_visual_official_loader_uses_strict_checkpoint_and_fp32(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """官方加载路径固定使用 pretrained=False、strict state dict 和 FP32。"""
    source = tmp_path / "dinov2"
    for relative in ("dinov2/__init__.py", "dinov2/hub/backbones.py", "dinov2/models/vision_transformer.py"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# test source\n", encoding="utf-8")
    (source / ".mememeow-dinov2-source-commit").write_text("7764ea0f912e53c92e82eb78a2a1631e92725fc8\n", encoding="ascii")
    weights = tmp_path / "model.pth"
    weights.write_bytes(b"checkpoint")

    calls: dict[str, object] = {}

    class FakeModel:
        """模拟官方模型最小接口，保留加载器关键契约。"""

        embed_dim = VISUAL_DIMENSIONS

        def load_state_dict(self, state_dict: object, *, strict: bool) -> None:
            """记录严格加载参数。"""
            calls["state_dict"] = state_dict
            calls["strict"] = strict

        def float(self) -> "FakeModel":
            """记录 FP32 转换。"""
            calls["float"] = True
            return self

    def builder(*, pretrained: bool) -> FakeModel:
        """模拟官方 DINOv2 ViT-B/14 构造函数。"""
        calls["pretrained"] = pretrained
        return FakeModel()

    fake_dinov2 = types.ModuleType("dinov2")
    fake_dinov2.__path__ = [str(source / "dinov2")]  # type: ignore[attr-defined]
    fake_hub = types.ModuleType("dinov2.hub")
    fake_hub.__path__ = [str(source / "dinov2" / "hub")]  # type: ignore[attr-defined]
    fake_backbones = types.ModuleType("dinov2.hub.backbones")
    fake_backbones.dinov2_vitb14 = builder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dinov2", fake_dinov2)
    monkeypatch.setitem(sys.modules, "dinov2.hub", fake_hub)
    monkeypatch.setitem(sys.modules, "dinov2.hub.backbones", fake_backbones)

    class FakeTorch:
        """只模拟安全 state dict 加载。"""

        @staticmethod
        def load(_path: str, *, map_location: str, weights_only: bool) -> object:
            """返回可被严格加载的字典。"""
            assert map_location == "cpu"
            assert weights_only is True
            return {"weight": 1}

    settings = types.SimpleNamespace(
        visual_model=VISUAL_MODEL_ID,
        visual_model_dimensions=VISUAL_DIMENSIONS,
        visual_preprocess_version=VISUAL_PREPROCESS_VERSION,
        visual_model_repo=source,
        visual_weights_path=weights,
        visual_weights_sha256=None,
    )
    runner = VisualModelRunner(settings)
    model = runner._load_official_model(FakeTorch(), weights)
    assert isinstance(model, FakeModel)
    assert calls == {"pretrained": False, "state_dict": {"weight": 1}, "strict": True, "float": True}


def test_local_visual_cli_reports_missing_runtime_environment() -> None:
    """CLI 缺少 task id 时只写稳定 JSON 错误到 stderr。"""
    script = Path("skills/research-meme-context/scripts/local_visual_match.py")
    environment = dict(os.environ)
    environment.pop("MEMEMEOW_AGENT_TASK_ID", None)
    environment.pop("MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL", None)
    environment.pop("MEMEMEOW_VISUAL_MATCH_INTERNAL_URL", None)
    result = subprocess.run([sys.executable, str(script), "--top-k", "2"], capture_output=True, text=True, env=environment, check=False)
    assert result.returncode != 0
    assert json.loads(result.stderr)["error"] == "agent_task_id_missing"


def test_local_visual_cli_reports_missing_manifest() -> None:
    """CLI 缺少固定 manifest 时返回稳定错误，不再尝试 callback。"""
    environment = dict(os.environ)
    environment["MEMEMEOW_AGENT_TASK_ID"] = "task-123"
    environment.pop("MEMEMEOW_AGENT_CANDIDATE_MANIFEST", None)
    environment.pop("MEMEMEOW_VISUAL_SEARCH_INTERNAL_URL", None)
    environment.pop("MEMEMEOW_AGENT_CALLBACK_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "skills/research-meme-context/scripts/local_visual_match.py"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode != 0
    assert json.loads(result.stderr)["error"] == "candidate_manifest_missing"


def test_local_visual_cli_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    """CLI 发现 manifest hash 不一致时拒绝返回候选事实。"""
    candidate_root = tmp_path / "candidates" / "task-123"
    candidate_root.mkdir(parents=True)
    (candidate_root / "manifest.json").write_text(
        json.dumps(
            {
                "protocol_version": 2,
                "snapshot_sha256": "f" * 64,
                "matched_at": "2026-08-31T00:00:00+00:00",
                "candidate_count": 0,
                "query": {
                    "meme_id": "query",
                    "image_sha256": "a" * 64,
                    "model": "dinov2_vitb14",
                    "dimensions": 768,
                    "preprocess_version": "dinov2-v1-gif-first-frame",
                },
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["MEMEMEOW_AGENT_TASK_ID"] = "task-123"
    environment["MEMEMEOW_AGENT_CANDIDATE_MANIFEST"] = str(candidate_root / "manifest.json")
    result = subprocess.run(
        [sys.executable, "skills/research-meme-context/scripts/local_visual_match.py"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode != 0
    assert json.loads(result.stderr)["error"] == "candidate_manifest_invalid"
