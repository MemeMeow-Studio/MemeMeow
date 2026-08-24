## Why

`api.py` 同时承载应用装配、图片服务和 `/search` 检索 HTTP 编排。检索入口只需要严格
请求模型、当前 scope 的 search service、Settings 的嵌入密钥和受控媒体 URL 映射，却被
入口模块中大量无关领域 helper 包围。阶段 2 需要抽取这个小而完整的公共核心边界，
同时冻结缓存未就绪、嵌入配置错误、LLM fallback 和媒体去重语义。

## What Changes

- 新增公共核心 `backend/search_http.py`，集中承载 `SearchRequest`、检索 handler 和结果
  媒体 URL 投影。
- `api.py` 保留 `SearchRequest`、`search_images` 与 `_media_for_meme` 的兼容 aliases/wrapper，
  通过 callback 注入原有 `_service`、错误构造和 metadata scope 解析。
- 保持 `POST /search` 的 path/method/tag/order、输入严格校验、`invalid_query`、
  `service_unavailable`、`cache_not_ready`、`configuration_missing`、`search_failed` 错误
  code/status、LLM fallback、结果去重与 `/media/{meme_id}` 映射。
- 增加路由、依赖方向、输入模型和检索 fallback 契约测试；不修改数据库、搜索服务算法、
  operation policy 或 Server adapter。

## Capabilities

### New Capabilities

无。该 change 只调整公共核心内部 HTTP 模块边界。

### Modified Capabilities

- `search-http`：冻结 `/search` 的 HTTP 输入、错误、缓存和媒体投影契约，不增加业务行为。

## Impact

影响公共核心 `api.py`、新增 `backend/search_http.py`、搜索/API 测试和 OpenSpec artifacts。
公共实现完成并验证后，Server 只通过精确开源 commit 的普通 Git merge 同步；不创建 Server
平行实现，不改数据库 schema、前端或第三方依赖。
