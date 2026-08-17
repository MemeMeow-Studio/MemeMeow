## Why

当前公共核心已经在数据库、文件和任务中保存 scope_id，但 API、生命周期和后台处理器仍将 local 作为隐式事实，并复用可变 scope 的进程级服务。宿主服务要安全承载多个用户 scope，必须先在开源核心收束 scope 的可信来源和生命周期边界。

## What Changes

- 新增可注入的 ScopeResolver 和统一 resolve_scope(request) 边界。应用工厂必须显式传入 resolver；仅开源模块级入口显式安装 LocalScopeResolver("local")，漏配 resolver 必须 fail-closed。
- 将公共业务请求绑定到可信 request scope；客户端字段、文件名和路径不能选择或覆盖 scope_id、user_id。
- 将异步任务和可信内部 callback 的 scope 事实来源固定为持久 Task.scope_id 或有效 claim，禁止从 payload、客户端字段或进程默认值推断。
- 将 scope-bound 服务改为请求或任务级环境，复用连接池、模型和 HTTP client 等重资源，禁止并发请求原地切换共享 service 的 scope。
- 保持开源的无鉴权、单用户 API 和 local 文件布局兼容；账户、鉴权、配额、订阅、付费和 operation policy 不属于本 change。

## Capabilities

### New Capabilities

- application-scope: 定义可信 scope 解析、开源 local 装配、fail-closed 应用入口、请求与任务 scope 事实来源及其隔离边界。

### Modified Capabilities

- scoped-persistence: 将公共核心中隐式固定的 local 绑定改为可信 request scope 或持久任务 scope，同时维持既有数据库和文件隔离契约。

## Impact

- 主要影响 api.py 的应用装配与路由、backend/database.py 的数据环境、backend/pg_services.py 的服务和任务协调、视觉/反向图片/合集服务及 Agent Runner 的 scope 传递。
- 需要 resolver、请求/任务隔离、Worker 重启和并发多 scope 的单元、API 与 PostgreSQL 集成测试；不会新增用户或计费实体，也不会实现鉴权和 operation policy。
- add-task-scoped-reverse-image-search、share-and-import-meme-collections 与 2026-08-15-compose-agent-executor 的外部协议继续由各自 change 维护。本 change 只提供它们需要的 scope 绑定机制，不重复定义反向图片审计、ZIP 格式或 executor 鉴权协议。
