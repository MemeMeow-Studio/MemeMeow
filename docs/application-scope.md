# 应用 Scope 边界

## 请求装配

FastAPI 必须通过 `create_app(scope_resolver=...)` 创建。公共核心只调用
`resolve_scope(request)`，不读取客户端 `scope_id`、`user_id`、路径前缀或普通
payload。开源模块级入口显式使用 `LocalScopeResolver("local")`；适配宿主应从
已完成认证的请求上下文注入自己的 resolver。resolver 缺失、返回空值、非法值或
抛出异常都以 `scope_resolution_failed` / `scope_unavailable` 失败，不回退到
`local`。

一次请求开始时 scope 被写入 `request.state.scope`，同时由
`ScopeServiceFactory.for_scope` 取得不可变 `ScopeServices`。Engine、连接池、只读
模型和安全可复用 HTTP client 属于应用级重资源；metadata、search、task、视觉和
反向图片服务绑定单一 scope，不在并发请求之间原地修改。local 继续使用
`MEMEMEOW_IMAGE_ROOT` 的扁平布局，其他 scope 的图片和反向图片缓存使用数据库
`Scope.storage_namespace` 派生的物理 namespace。

适配宿主可以注入自己的 factory，也可以省略 factory 复用核心默认的进程级
scope-aware Worker；只要 resolver 不是显式 `LocalScopeResolver("local")`，两条路径在
lifespan 启动时都不会调用 `for_scope("local")`、执行 local storage preflight 或写入
`app.state.metadata/tasks`。默认 factory 只在请求或任务认领后按可信 scope 懒创建 facade。
宿主数据库可以没有 local scope；resolver 返回的第一个可信 scope 才是业务装配入口。
只有开源模块级入口显式安装 `LocalScopeResolver("local")` 时，才创建 local 服务并执行
local 文件预检。

## 任务与内部回调

创建任务时由服务端把当前 request scope 写入 `Task.scope_id`，payload 中的 scope
或 user 字段会被丢弃。Worker 认领后只从任务行和有效 claim 恢复 scope；heartbeat、
重试、fencing、视觉写回、Agent 子任务、批次 finalizer、缓存和 provenance 均在
该 scope 环境执行。一个进程只有一个 `PostgresTaskWorkerManager`，共享线程池、
handler registry、Agent lane 背压和恢复扫描；scope facade 只在请求或认领任务时按需
创建，不按历史 scope 数量启动 Worker。无效 Task scope 由启动诊断标记为
`task_scope_invalid`，不会猜测为 local。

联网反向图片 callback 只接收 task ID，跳过公共用户 request scope 绑定，先从任务
控制面恢复 scope，再按任务类型、运行状态和 claim 条件处理。视觉候选不再通过 callback，
而是在 Agent 启动前由服务端冻结候选图片清单。callback 不信任自报 scope，
并要求独立 callback verifier 验证短期任务凭据；认证发生在读取 multipart/JSON body、
查询 Task 或创建临时文件之前。开源入口可使用显式配置的 HMAC callback secret，secret
缺失或 verifier 异常时 callback 保持不可用，不回退到无认证。宿主公网部署仍必须在代理
或宿主层提供内部 endpoint 的网络隔离和路由暴露控制。

Compose executor 继续使用固定 HTTP/token 协议，Agent 只获得受控的任务相对图片
输入，不接收客户端 scope、user 或任意物理路径。non-local scope 必须由适配宿主
配置安全 `agent_input_provider`；未配置或返回非普通文件时任务以
`agent_input_provider_unavailable` 稳定失败。

## 部署、双 Scope staging 与回滚

1. 先在 staging 数据库创建两个 scope，使用可信 resolver 将请求分别映射到 scope-a
   和 scope-b，验证同名图片、合集、任务、媒体 URL、搜索 generation、反向图片缓存
   和文件 namespace 互不可见。
2. 并发运行上传、列表、媒体、搜索、任务认领、过期 claim、重试和 callback 测试，
   检查任务 scope 与 claim generation 不发生漂移。
3. non-local Agent 输入 provider、内部服务认证和 executor 健康检查全部通过后，
   才切换公网流量；本 change 不实现账户、登录、订阅、计费、配额或 operation policy。

回滚前先停止 non-local 流量并收束活动任务，保留数据库记录和 namespace。旧版本不
得把 non-local 任务映射到 local；恢复时先升级到支持 scope 的 Worker，再重新认领
过期任务。

## 宿主同步

公共 scope 核心在开源仓库 `/home/infstellar/vscode/MemeMeow` 维护；宿主服务器只同步
本文件和 `backend/`、`api.py` 中不涉及账户、鉴权、配额、订阅或计费的核心改动。宿主
宿主不得复制 local 全局 service，也不得在同步时把 resolver、`Task.scope_id` 或
`agent_input_provider` 改成从客户端字段读取。每次同步后先运行 local 回归和双 scope
staging，再启用宿主流量；账户和 operation policy 仍由宿主边界单独实现。
