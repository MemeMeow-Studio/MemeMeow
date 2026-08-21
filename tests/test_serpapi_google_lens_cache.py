"""SerpApi Google Lens Skill 缓存的离线回归测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def lens_module():
    """从 Skill 目录加载缓存脚本，避免测试依赖全局安装。"""
    root = Path(__file__).resolve().parents[1]
    candidates = (
        root / "skills/research-meme-context/scripts/serpapi_google_lens.py",
        root / ".agents/skills/research-meme-context/scripts/serpapi_google_lens.py",
    )
    script = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    assert script.is_file()
    specification = importlib.util.spec_from_file_location("serpapi_google_lens", script)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def make_request(lens_module, tmp_path: Path):
    """构造独立的最小图片输入和 Lens 请求。"""
    image = tmp_path / "meme.jpg"
    image.write_bytes(b"not-a-real-jpeg-but-a-stable-test-input")
    return lens_module.LensRequest(image_path=image)


def successful_response(title: str = "候选") -> dict[str, object]:
    """生成带需脱敏字段的成功响应，验证缓存不会持久化归档标识。"""
    return {
        "search_metadata": {
            "status": "Success",
            "id": "private-search-id",
            "json_endpoint": "https://serpapi.example/private.json",
            "diagnostic": "request used test-key",
        },
        "visual_matches": [
            {
                "title": title,
                "link": "https://example.com/candidate",
                "serpapi_link": "https://serpapi.example/private-link",
                "about_page_serpapi_link": "https://serpapi.example/also-private",
            }
        ],
    }


def test_successful_result_is_reused_and_sanitized(lens_module, tmp_path: Path):
    """同一图片和参数第二次调用命中缓存，且快照不含 SerpApi 私有标识。"""
    request = make_request(lens_module, tmp_path)
    calls = []

    def fetch(_request, _api_key):
        calls.append(1)
        return successful_response()

    first = lens_module.run_lens_search(request, "test-key", tmp_path / "cache", fetch_result=fetch)
    second = lens_module.run_lens_search(request, "test-key", tmp_path / "cache", fetch_result=fetch)

    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"
    assert len(calls) == 1
    serialized = json.dumps(second, ensure_ascii=False)
    assert "private-search-id" not in serialized
    assert "private-link" not in serialized
    assert "also-private" not in serialized
    assert "test-key" not in serialized


def test_empty_result_expires_but_successful_refresh_keeps_history(lens_module, tmp_path: Path):
    """空结果在短期内复用，过期后重新请求；刷新成功时历史快照不会丢失。"""
    request = make_request(lens_module, tmp_path)
    cache_root = tmp_path / "cache"
    started = datetime(2026, 8, 10, tzinfo=UTC)
    calls = []

    def empty_fetch(_request, _api_key):
        calls.append("empty")
        return {"search_metadata": {"status": "Success"}, "visual_matches": []}

    first = lens_module.run_lens_search(request, "test-key", cache_root, now=started, fetch_result=empty_fetch)
    second = lens_module.run_lens_search(request, "test-key", cache_root, now=started + timedelta(days=1), fetch_result=empty_fetch)
    third = lens_module.run_lens_search(request, "test-key", cache_root, now=started + timedelta(days=4), fetch_result=lambda *_: successful_response("更新候选"))

    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "hit"
    assert third["cache"]["status"] == "refresh"
    assert calls == ["empty"]
    record = next(cache_root.glob("*.json"))
    assert len(json.loads(record.read_text(encoding="utf-8"))["snapshots"]) == 2


def test_failed_provider_result_is_not_cached(lens_module, tmp_path: Path):
    """失败响应抛出异常且不会写入可被后续任务误复用的缓存。"""
    request = make_request(lens_module, tmp_path)
    cache_root = tmp_path / "cache"

    with pytest.raises(lens_module.SerpApiError):
        lens_module.run_lens_search(
            request,
            "test-key",
            cache_root,
            fetch_result=lambda *_: {"search_metadata": {"status": "Error"}, "error": "provider failed"},
        )

    assert not list(cache_root.glob("*.json"))


def test_concurrent_requests_share_one_provider_call(lens_module, tmp_path: Path):
    """同一缓存键的并发任务在文件锁内二次检查，只有首个任务调用供应商。"""
    request = make_request(lens_module, tmp_path)
    cache_root = tmp_path / "cache"
    calls = []

    def fetch(_request, _api_key):
        calls.append(1)
        time.sleep(0.05)
        return successful_response()

    def run():
        return lens_module.run_lens_search(request, "test-key", cache_root, fetch_result=fetch)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _: run(), range(2)))

    assert len(calls) == 1
    assert {output["cache"]["status"] for output in outputs} == {"miss", "hit"}


def test_thin_cli_omits_request_id_by_default_but_keeps_legacy_option(lens_module) -> None:
    """薄 CLI 默认让服务端生成权威 ID，同时保留旧显式参数。"""
    omitted = lens_module.parse_args(["image.png", "--task-id", "task-a"])
    explicit = lens_module.parse_args(["image.png", "--task-id", "task-a", "--request-id", "legacy-id"])
    assert omitted.request_id is None
    assert explicit.request_id == "legacy-id"

    body, _content_type = lens_module._multipart({"task_id": "task-a"}, b"image", "image.png")
    assert b"name=\"request_id\"" not in body


def test_thin_cli_prints_authoritative_response_without_provider_credentials(lens_module, tmp_path: Path, monkeypatch, capsys) -> None:
    """CLI 原样输出后端权威 request ID，环境只需要 callback token。"""
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    monkeypatch.setenv("MEMEMEOW_AGENT_CALLBACK_TOKEN", "task-callback-token")
    monkeypatch.setenv("MEMEMEOW_REVERSE_IMAGE_INTERNAL_URL", "http://internal.test/reverse-image/search")

    class Response:
        """最小 HTTP 响应替身。"""

        def read(self) -> bytes:
            """返回后端供应商无关响应。"""
            return json.dumps({"request_id": "cb-authoritative", "provider": {"called": False}}).encode()

    def fake_urlopen(request, timeout):
        """检查 callback 请求只使用 Runner 注入的地址和凭据。"""
        assert request.full_url == "http://internal.test/reverse-image/search"
        assert request.get_header("X-mememeow-callback") == "task-callback-token"
        assert timeout == 60
        assert b'name="request_id"' not in request.data
        return Response()

    monkeypatch.setattr(lens_module, "urlopen", fake_urlopen)
    assert lens_module.main([str(image), "--task-id", "task-a"]) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == "cb-authoritative"
