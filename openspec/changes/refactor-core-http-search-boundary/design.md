## Context

当前 `api.py:218-223` 定义 `SearchRequest`，`api.py:447-454` 定义 `_media_for_meme`，
`api.py:1998-2028` 定义 `/search` handler。handler 读取当前 scope-bound search/metadata
service，调用 Settings embedding key，并把 service 返回的 meme id 转为去重媒体 URL。

## Goals / Non-Goals

**Goals:**

- 把 SearchRequest、搜索 HTTP 编排和媒体 URL投影移到不依赖 `api.py` 的公共模块。
- 通过 callback 注入 `service`、`media_for_meme` 和统一 `_error`，复用入口既有 scope、
  local fixture fallback 和错误响应格式。
- 保持 route metadata、严格请求校验、错误码、LLM fallback、结果上限和重复媒体过滤。
- 让 `api.py` 保留旧 import/handler/helper 兼容名称，且不改变 `/generate-cache` 或其它路由。

**Non-Goals:**

- 不移动 search engine、Postgres repository、cache generation task 或 operation policy。
- 不改变 search query normalization、embedding 调用、scope 解析、媒体文件安全检查或
  Settings 配置字段。
- 不添加新的 HTTP path、method、错误码、数据库事实或第三方依赖。

## Decisions

### 1. Callback 注入入口边界

`backend.search_http.search_images()` 接受 `service(request, name)`、`media_for_meme(request,
meme_id)` 和 `error(status, code, message)` callback。新模块只编排 HTTP 输入和 search
service 结果，不复制 scope 或 metadata repository 逻辑，也不反向 import `api.py`。

### 2. 保留 SearchRequest 旧 alias 与 route decorator

`SearchRequest` 在新模块定义并由 `api.py` 显式 re-export。`api.py` 继续在原位置保留
`@app.post("/search", tags=["search"])` decorator，由薄 wrapper 调用新 handler，确保
FastAPI 路由名称、顺序和公开 schema 不变。

### 3. 媒体映射仍由入口 helper 承担

`_media_for_meme` 继续保留在 `api.py`，因为它依赖 `MetadataError` 和入口 `_service` 的
legacy local fixture fallback；新 handler 通过 callback 使用它。这样抽取不会复制或改变
metadata scope 语义。

## Dependency / Rollback

依赖方向为：

```text
backend.search_http -> FastAPI / Pydantic / backend.config
api.py -> backend.search_http
```

回滚时恢复 `api.py` 原 SearchRequest 与 handler，实现删除新模块、测试和 change artifacts；
不涉及 Server 或公共搜索领域实现。

## Review Checklist

- 新模块不导入 `api.py` 或 `server_api`。
- `/search` 仍只有一个 POST route，GET 继续 405，route tag/name/order 不变。
- 空 query、无 service、cache 未就绪、embedding 配置失败和 search/LLM fallback 错误 code/status 不变。
- media mapper 只返回当前 scope 可解析的 `/media/{id}`，重复/未知结果不泄露内部标识。
