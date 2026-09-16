"""Google Vision Web Detection provider 的离线契约测试。"""

from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.reverse_image import (
    GoogleVisionWebDetectionProvider,
    ReverseImageCache,
    ReverseImageError,
    ReverseImageProviderBinding,
    ReverseImageRequest,
    ReverseImageService,
    SerpApiGoogleLensProvider,
    _fingerprint,
    _is_empty,
    _normalize_google_web_detection,
)


def _request() -> ReverseImageRequest:
    """构造不依赖图片解码的 provider 输入。"""
    return ReverseImageRequest(image=b"test-image", filename="meme.png", task_id="task")


def test_cache_identity_isolated_by_provider_engine_and_variant() -> None:
    """同一图片的 provider、engine 或变体变化时必须产生不同缓存键。"""
    request = _request()
    image_sha = "a" * 64
    default_identity = request.identity(image_sha)
    provider_identity = request.identity(image_sha, provider="serpapi", engine="google_lens")
    variant_identity = request.identity(image_sha, cache_variant="alternate")

    assert default_identity != provider_identity
    assert default_identity != variant_identity
    assert _fingerprint(default_identity) != _fingerprint(variant_identity)
    assert set(default_identity) == {"provider", "engine", "cache_variant", "image_sha256", "search_type", "language", "country", "query", "auto_crop"}


def test_provider_binding_freezes_identity_and_call_target(tmp_path: Path) -> None:
    """宿主绑定固定保存非秘密身份，并由服务使用同一个调用对象。"""

    def search(_request: ReverseImageRequest) -> dict[str, object]:
        """返回合法空结果，供绑定调用对象测试使用。"""
        return {"visual_matches": []}

    binding = ReverseImageProviderBinding(name="host_provider", engine="visual_search", cache_variant="verified", search=search)
    settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project=None,
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    service = ReverseImageService(settings, SimpleNamespace(), provider_binding=binding)

    assert service.provider_name == "host_provider"
    assert service.provider_engine == "visual_search"
    assert service.provider_cache_variant == "verified"
    assert service._provider() is search
    with pytest.raises(FrozenInstanceError):
        binding.name = "changed"  # type: ignore[misc]


def test_sync_provider_adapter_runs_off_event_loop(tmp_path: Path) -> None:
    """同步 Provider 只在线程中运行，等待期间事件循环仍能调度。"""
    loop_thread = threading.get_ident()
    provider_thread: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def search(_request: ReverseImageRequest) -> dict[str, object]:
        provider_thread.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return {"visual_matches": []}

    settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project=None,
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    service = ReverseImageService(settings, SimpleNamespace(), provider_binding=ReverseImageProviderBinding("host", "engine", "variant", search))

    async def exercise() -> None:
        task = asyncio.create_task(service._network_search_async(_request()))
        ticks = 0
        while not started.is_set():
            ticks += 1
            await asyncio.sleep(0)
        release.set()
        assert await task == {"visual_matches": []}
        assert ticks > 0

    asyncio.run(exercise())
    assert provider_thread and provider_thread[0] != loop_thread


def test_started_sync_provider_cancellation_waits_for_worker_completion(tmp_path: Path) -> None:
    """Service 延迟同步 Provider 的请求取消，直到线程得到明确结果。"""
    started = threading.Event()
    release = threading.Event()

    def search(_request: ReverseImageRequest) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return {"visual_matches": []}

    settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project=None,
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    service = ReverseImageService(settings, SimpleNamespace(), provider_binding=ReverseImageProviderBinding("host", "engine", "settled", search))

    async def exercise() -> None:
        task = asyncio.create_task(service._settle_started_provider(_request()))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        response, error, cancellation = await task
        assert response == {"visual_matches": []}
        assert error is None
        assert isinstance(cancellation, asyncio.CancelledError)

    asyncio.run(exercise())


def test_async_cache_lock_wait_is_cancellable(tmp_path: Path) -> None:
    """异步等待同键文件锁时可以取消，既有持锁者不受影响。"""
    cache = ReverseImageCache(tmp_path / "cache")

    async def exercise() -> None:
        with cache.lock("same-key"):
            entered = False

            async def waiter() -> None:
                nonlocal entered
                async with cache.lock_async("same-key", retry_seconds=0.001):
                    entered = True

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert entered is False
        async with cache.lock_async("same-key", retry_seconds=0.001):
            pass

    asyncio.run(exercise())


def test_native_async_provider_is_awaited_without_worker_thread(tmp_path: Path) -> None:
    """原生异步 Provider 在当前事件循环中执行，不经过线程兼容层。"""
    called_threads: list[int] = []

    class AsyncProvider:
        async def search_async(self, _request: ReverseImageRequest) -> dict[str, object]:
            called_threads.append(threading.get_ident())
            await asyncio.sleep(0)
            return {"visual_matches": []}

    settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project=None,
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    service = ReverseImageService(settings, SimpleNamespace(), provider_binding=ReverseImageProviderBinding("host", "engine", "variant", AsyncProvider()))
    current_thread = threading.get_ident()

    assert asyncio.run(service._network_search_async(_request())) == {"visual_matches": []}
    assert called_threads == [current_thread]


def test_sync_search_rejects_running_event_loop(tmp_path: Path) -> None:
    """异步调用方误用同步入口时明确报错，不能静默阻塞事件循环。"""
    settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project=None,
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    service = ReverseImageService(settings, SimpleNamespace(), provider=lambda _request: {"visual_matches": []})

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="reverse_image_search_async_required"):
            service.search(_request())

    asyncio.run(exercise())


def test_service_selects_only_the_configured_provider(tmp_path: Path) -> None:
    """默认选择 Google，显式 SerpApi 时不创建 Google provider。"""
    google_settings = SimpleNamespace(
        reverse_image_provider="google_vision",
        google_cloud_project="test-project",
        google_application_credentials=None,
        serpapi_api_key=None,
        data_root=tmp_path / "data",
        reverse_image_cache_root=tmp_path / "cache",
    )
    google_service = ReverseImageService(google_settings, SimpleNamespace())
    assert isinstance(google_service._provider(), GoogleVisionWebDetectionProvider)

    serpapi_settings = SimpleNamespace(**{**google_settings.__dict__, "reverse_image_provider": "serpapi", "serpapi_api_key": "test-key"})
    serpapi_service = ReverseImageService(serpapi_settings, SimpleNamespace())
    assert isinstance(serpapi_service._provider(), SerpApiGoogleLensProvider)


def test_google_result_keeps_provider_groups_and_common_candidates() -> None:
    """Google 分组转换为通用候选，同时保留原始专属字段。"""
    result = _normalize_google_web_detection(
        {
            "pagesWithMatchingImages": [{"pageTitle": "网页标题", "url": "https://example.com/page"}],
            "fullMatchingImages": [{"url": "https://example.com/full.jpg"}],
            "partialMatchingImages": [{"url": "https://example.com/partial.jpg"}],
            "visuallySimilarImages": [{"url": "https://example.com/similar.jpg"}],
            "webEntities": [{"entityId": "/m/abc", "description": "实体", "score": 0.8}],
            "bestGuessLabels": [{"label": "标签", "languageCode": "zh"}],
        }
    )
    assert result["visual_matches"]
    assert result["exact_matches"] == [{"link": "https://example.com/full.jpg", "source": "https://example.com/full.jpg"}]
    assert result["web_entities"] == [{"description": "实体", "entity_id": "/m/abc", "score": 0.8}]
    assert result["best_guess_labels"] == [{"label": "标签", "language": "zh"}]
    assert result["google_vision_web_detection"]["pages_with_matching_images"][0]["pageTitle"] == "网页标题"
    assert _is_empty(_normalize_google_web_detection({})) is True


def test_google_provider_posts_web_detection_request_without_leaking_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 使用授权会话和项目 header，返回结果不包含请求认证材料。"""
    captured: dict[str, object] = {}

    class FakeResponse:
        ok = True

        def json(self) -> dict[str, object]:
            return {"responses": [{"webDetection": {"bestGuessLabels": [{"label": "标签"}]}}]}

    class FakeSession:
        def __init__(self, credentials: object):
            captured["credentials"] = credentials

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float) -> FakeResponse:
            captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", FakeSession)
    provider = GoogleVisionWebDetectionProvider(project_id="test-project")
    monkeypatch.setattr(provider, "_credentials", lambda: (object(), "test-project"))

    result = provider.search(_request())
    body = captured["json"]
    assert captured["url"] == "https://vision.googleapis.com/v1/images:annotate"
    assert captured["headers"] == {"x-goog-user-project": "test-project"}
    assert body["requests"][0]["features"] == [{"type": "WEB_DETECTION", "maxResults": 20}]
    assert base64.b64decode(body["requests"][0]["image"]["content"]) == b"test-image"
    assert result["best_guess_labels"] == [{"label": "标签"}]
    assert captured["closed"] is True
    assert "credentials" not in repr(result)


def test_google_provider_http_failure_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google HTTP 失败只返回稳定 provider 错误，不泄露响应正文。"""

    class FakeResponse:
        ok = False

        def json(self) -> dict[str, object]:
            return {"error": {"message": "private response body"}}

    class FakeSession:
        def __init__(self, _credentials: object):
            pass

        def post(self, *_args: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", FakeSession)
    provider = GoogleVisionWebDetectionProvider(project_id="test-project")
    monkeypatch.setattr(provider, "_credentials", lambda: (object(), "test-project"))
    with pytest.raises(ReverseImageError) as error:
        provider.search(_request())
    assert error.value.code == "reverse_image_provider_unavailable"
    assert "private response body" not in str(error.value)
