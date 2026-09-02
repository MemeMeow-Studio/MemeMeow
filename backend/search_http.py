"""公共核心 `/search` HTTP 边界。

本模块负责检索请求模型、缓存/嵌入错误投影、LLM fallback 和媒体 URL 结果去重。scope
service、媒体解析和统一 HTTP 错误由入口通过 callback 注入，避免反向依赖 ``api.py``。
"""

from __future__ import annotations

from collections.abc import Callable
import math
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class _SearchRequestModel(BaseModel):
    """检索 HTTP 请求模型基类，拒绝客户端提交未定义字段。"""

    model_config = ConfigDict(extra="forbid")


class SearchRequest(_SearchRequestModel):
    """规范检索请求。"""

    query: str = Field(min_length=1, max_length=500)
    n_results: StrictInt = Field(default=5, ge=1, le=30)
    llm_enhance: bool = False


ThumbnailProjectionProvider = Callable[[Request, str], dict[str, object] | None]


async def search_images(
    request: Request,
    payload: SearchRequest,
    *,
    service: Callable[[Request, str], Any],
    media_for_meme: Callable[[Request, str], str | None],
    error: Callable[[int, str, str], HTTPException],
    thumbnail_for_meme: ThumbnailProjectionProvider | None = None,
) -> dict[str, object]:
    """执行 scope-bound 检索并把结果投影为受控媒体 URL。

    关键输入是严格校验后的 query、结果数量和 LLM 开关；service、media_for_meme 与
    error callback 由应用入口注入，输出只包含当前 scope 可解析且去重的媒体路径。调用
    场景是公共 `POST /search` handler，LLM 增强失败时按既有协议只 fallback 一次。
    """
    query = payload.query.strip()
    if not query:
        raise error(400, "invalid_query", "query 不能为空")
    engine = service(request, "search")
    if engine is None:
        raise error(503, "service_unavailable", "检索服务未初始化")
    if not engine.has_cache():
        raise error(503, "cache_not_ready", "检索缓存尚未就绪")
    settings = request.app.state.settings
    try:
        results = engine.search(query, payload.n_results, api_key=settings.embedding_api_key, use_llm=payload.llm_enhance)
    except Exception as exc:  # noqa: BLE001 - 搜索 provider 的诊断不直接暴露给客户端
        if "embedding_not_configured" in str(exc):
            raise error(503, "configuration_missing", "嵌入模型配置未完成") from exc
        if not payload.llm_enhance:
            raise error(500, "search_failed", "检索失败") from exc
        try:
            results = engine.search(query, payload.n_results, api_key=settings.embedding_api_key, use_llm=False)
        except Exception as fallback_exc:  # noqa: BLE001 - fallback 失败仍使用稳定错误码
            if "embedding_not_configured" in str(fallback_exc):
                raise error(503, "configuration_missing", "嵌入模型配置未完成") from fallback_exc
            raise error(500, "search_failed", "检索失败") from fallback_exc
    # 保留原入口的 service 访问顺序，确保 metadata scope/service 不会被隐式放宽。
    service(request, "metadata")
    mapped: list[str] = []
    result_media: list[dict[str, object]] = []
    for item in results or []:
        if isinstance(item, tuple) and len(item) == 2:
            meme_id, raw_score = item
            score = float(raw_score) if isinstance(raw_score, (int, float)) else None
        elif isinstance(item, str):
            meme_id, score = item, None
        else:
            continue
        if not isinstance(meme_id, str):
            continue
        media = media_for_meme(request, meme_id)
        if media and media not in mapped:
            mapped.append(media)
            if thumbnail_for_meme is not None:
                projection = thumbnail_for_meme(request, meme_id)
                entry: dict[str, object] = {"meme_id": meme_id, "media_url": media, "thumbnail": projection or {"status": "pending", "media_url": None}}
                if score is not None and math.isfinite(score):
                    entry["score"] = score
                result_media.append(entry)
        if len(mapped) >= payload.n_results:
            break
    response: dict[str, object] = {"results": mapped}
    if thumbnail_for_meme is not None:
        response["result_media"] = result_media
    return response


__all__ = ["SearchRequest", "search_images"]
