## ADDED Requirements

### Requirement: Cache task HTTP route remains compatible

系统 MUST 继续注册单个 `POST /generate-cache` 路由，保持 `202`、`tasks` tag、handler 名称
和既有路由相对顺序；未注册 method MUST 返回 `405`。

#### Scenario: Route snapshot remains stable

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** 路由表包含单个 `POST /generate-cache` route，位于 `/search` 之后、`/tasks` 读取
  路由之前
- **AND** GET 请求不执行 service 或 task 提交并返回 `405`

### Requirement: Cache task readiness and response remain stable

handler MUST 先读取当前 scope 的 search service；缺失时返回 `503/service_unavailable`，且
不得调用 task service。就绪时 MUST 以空 payload 提交 `cache_generation` 任务，并返回
`{"task_id", "task_type", "status"}`，不暴露 task payload 或内部对象。

#### Scenario: Search service unavailable

- **WHEN** 当前 scope 没有 search service
- **THEN** 返回既有 `503/service_unavailable`
- **AND** task service 不被读取或调用

#### Scenario: Cache task submitted

- **WHEN** 当前 scope search service 存在
- **THEN** task service 只提交一次 `cache_generation` 和空 payload
- **AND** 返回 `202` route response 所需的三个稳定字段

### Requirement: Module dependency remains one-way

公共核心 cache task HTTP 模块 MUST NOT import `api.py` 或 Server 入口；scope/service 与错误
投影必须通过入口 callback 注入。

#### Scenario: Legacy import remains available

- **WHEN** 调用方从 `api` 导入 `generate_cache`
- **THEN** 该名称仍可调用并保留原 route wrapper 语义
