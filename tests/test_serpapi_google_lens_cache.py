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
    script = Path(__file__).resolve().parents[1] / ".agents/skills/research-meme-context/scripts/serpapi_google_lens.py"
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
