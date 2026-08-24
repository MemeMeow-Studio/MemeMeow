"""公共核心 `/search` HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import api
import backend.search_http as search_http


def _request() -> SimpleNamespace:
    """构造不启动 lifespan 的最小检索请求。"""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(embedding_api_key="embedding-key"))),
        state=SimpleNamespace(),
    )


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def test_search_http_boundary_preserves_aliases_and_one_way_dependency() -> None:
    """新模块与旧入口共享 SearchRequest，且不反向依赖 api。"""
    assert api.SearchRequest is search_http.SearchRequest
    assert api.search_images.__name__ == "search_images"

    source = Path(search_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "api" not in imported_modules
    assert "server_api" not in imported_modules


def test_search_route_snapshot_keeps_public_metadata_and_position() -> None:
    """`/search` 继续是单个公开 POST search 路由，并位于 config 后。"""
    relevant = [route for route in api.app.routes if getattr(route, "path", None) in {"/config", "/search", "/generate-cache"}]
    assert [route.path for route in relevant] == ["/config", "/search", "/generate-cache"]
    search_route = relevant[1]
    assert search_route.methods == {"POST"}
    assert search_route.name == "search_images"
    assert search_route.tags == ["search"]
    assert sum(getattr(route, "path", None) == "/search" for route in api.app.routes) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "query", "n_results": 0},
        {"query": "query", "n_results": -1},
        {"query": "query", "n_results": "3"},
        {"query": "query", "unknown": True},
    ],
)
def test_search_request_keeps_strict_validation(payload: dict[str, object]) -> None:
    """搜索请求继续拒绝越界、字符串数字和未知字段。"""
    with pytest.raises(ValidationError):
        search_http.SearchRequest.model_validate(payload)


def test_search_projection_preserves_llm_fallback_and_media_dedupe() -> None:
    """LLM 失败仍以同一 query fallback，并只返回去重后的可映射媒体 URL。"""
    request = _request()
    calls: list[tuple[str, bool]] = []

    class Search:
        """记录检索调用并模拟 LLM 首次失败。"""

        def has_cache(self) -> bool:
            """返回缓存已就绪。"""
            return True

        def search(self, query: str, _limit: int, *, api_key: str, use_llm: bool) -> list[str]:
            """记录 query/LLM 开关并在增强模式抛出稳定模拟错误。"""
            assert api_key == "embedding-key"
            calls.append((query, use_llm))
            if use_llm:
                raise RuntimeError("llm unavailable")
            return ["meme-a", "meme-a", "unknown", 7]  # type: ignore[list-item]

    services = {"search": Search(), "metadata": object()}
    media_calls: list[str] = []

    def service(_request: object, name: str) -> object:
        """按名称返回当前 scope 的测试 service。"""
        return services[name]

    def media_for_meme(_request: object, meme_id: str) -> str | None:
        """只映射一个稳定 meme id，模拟未知成员被过滤。"""
        media_calls.append(meme_id)
        return "/media/meme-a" if meme_id == "meme-a" else None

    payload = asyncio.run(
        search_http.search_images(
            request,
            search_http.SearchRequest(query=" 原始查询 ", n_results=3, llm_enhance=True),
            service=service,
            media_for_meme=media_for_meme,
            error=_error,
        )
    )
    assert calls == [("原始查询", True), ("原始查询", False)]
    assert payload == {"results": ["/media/meme-a"]}
    assert media_calls == ["meme-a", "meme-a", "unknown"]


@pytest.mark.parametrize(
    ("service_value", "expected_code"),
    [(None, "service_unavailable"), (SimpleNamespace(has_cache=lambda: False), "cache_not_ready")],
)
def test_search_service_boundaries_fail_before_metadata_mapping(service_value: object, expected_code: str) -> None:
    """缺少 service 或 cache 未就绪时稳定失败且不调用 metadata/media。"""
    request = _request()
    calls: list[str] = []

    def service(_request: object, name: str) -> object:
        """记录 service 访问并返回指定 search fixture。"""
        calls.append(name)
        return service_value if name == "search" else object()

    def media_for_meme(_request: object, _meme_id: str) -> str:
        """不应在前置失败分支被调用。"""
        raise AssertionError("media mapping must not run")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            search_http.search_images(
                request,
                search_http.SearchRequest(query="query"),
                service=service,
                media_for_meme=media_for_meme,
                error=_error,
            )
        )
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == expected_code
    assert calls == ["search"]
