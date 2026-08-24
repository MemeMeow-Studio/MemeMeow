## ADDED Requirements

### Requirement: Search HTTP route and input remain compatible

系统 MUST 继续注册公开 `POST /search` 路由，保持 `search` tag、handler 名称、路由相对
顺序和未注册 method 的 `405` 行为。请求 MUST 只接受严格模型字段 `query`、`n_results`
和 `llm_enhance`，并保持长度、正整数和额外字段校验。

#### Scenario: Route snapshot remains stable

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** 路由表包含单个 `POST /search` route，且该 route 位于 `/config` 之后、
  `/generate-cache` 之前
- **AND** `GET /search` 继续返回 `405`

#### Scenario: Invalid input stays fail-closed

- **WHEN** 请求提交空 query、超限/非整数 `n_results`、未知字段或布尔之外的无效值
- **THEN** 系统返回既有请求校验错误，不调用 search service 或 metadata service

### Requirement: Search execution and response projection remain stable

`/search` MUST 在 query trim 后拒绝空值，依次检查 search service、cache readiness，再调用
embedding search。搜索异常必须保持 `configuration_missing`、`search_failed` 的映射；
`llm_enhance=true` 首次失败时只允许以 `use_llm=false` 对同一 query 做一次 fallback。响应
只能返回去重后的当前 scope 媒体 URL，最多返回 `n_results` 项。

#### Scenario: Cache and service boundaries

- **WHEN** search service 缺失或 cache 未就绪
- **THEN** 返回 `503/service_unavailable` 或 `503/cache_not_ready`
- **AND** 不读取 embedding key、不调用 metadata mapper

#### Scenario: LLM fallback preserves query

- **WHEN** enhanced search 失败且 `llm_enhance=true`
- **THEN** 以原始 trim 后 query 和 `use_llm=false` 只重试一次
- **AND** fallback 成功返回正常媒体结果，二次失败按既有错误码返回

#### Scenario: Unknown and duplicate ids are filtered

- **WHEN** search service 返回重复、非字符串或当前 scope 不存在的 meme id
- **THEN** 响应只包含可映射且去重的 `/media/{meme_id}` URL
- **AND** 不泄露原始未知标识或内部文件路径

### Requirement: Search module dependency direction stays one-way

公共核心 search HTTP 模块 MUST NOT import `api.py` 或 Server 入口；scope/service 和错误
投影必须通过入口 callback 注入，不能建立跨请求 singleton 或绕过 scope 校验。

#### Scenario: Legacy Python imports remain available

- **WHEN** 调用方从 `api` 导入 `SearchRequest`、`search_images` 或 `_media_for_meme`
- **THEN** 这些兼容名称仍可调用并对应当前实现
