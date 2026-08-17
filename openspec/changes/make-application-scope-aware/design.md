## Context

当前 PostgreSQL schema 已为 Meme、合集、向量、任务和反向图片用量事件保存 scope_id，DatabaseResources.environment(scope_id) 和 BlobStore 也具备 scope-bound 基础。但 api.py 仍创建固定 local 的进程级业务服务，多个路由和后台处理器仍直接调用 environment("local")，而 PostgresTaskService 的 Worker、heartbeat 和 finalizer 以自身的 self.scope 工作。

本设计把 Engine、连接池、只读模型配置和外部 HTTP client 保持为应用级重资源，把 scope 作为不可变的请求或任务上下文传递给轻量服务环境。现有 ScopeContext、复合外键、任务 claim generation 和 local BlobStore 映射是实现基础；不增加用户或账户模型。

## Goals / Non-Goals

**Goals:**

- 在应用边界显式装配 resolver，并由唯一的开源模块级入口显式安装 LocalScopeResolver("local")。
- 使公共请求、任务 Worker 和内部 task callback 使用不可伪造的 scope 事实来源。
- 在不复制连接池、模型客户端或线程池的前提下，让 scope-bound 服务可并发运行。
- 保持 local API、文件布局和三个相关 active changes 的既有外部协议。

**Non-Goals:**

- 不实现账户、登录、会话、鉴权、operation policy、配额、订阅或付费。
- 不建立 workspace、RBAC、scope 管理 API 或客户端 scope 选择能力。
- 不重新定义反向图片审计、合集 ZIP 格式、视觉模型或 executor 服务间认证；它们由现有或后续 change 负责。
- 不强制迁移现有 local 数据布局。

## Decisions

### 1. 使用单一 resolver 边界和显式应用工厂

新增轻量 scope 模块，定义 ScopeResolver 协议、LocalScopeResolver、ScopeResolutionError 和 resolve_scope(request)。resolver 只从可信请求上下文返回已校验的 ScopeContext，不读取客户端提交的 scope_id、user_id 或文件路径。

应用工厂首先提供 `create_app(*, scope_resolver, service_factory=None)`。后续 operation policy、Agent callback 和 Worker change 只能通过同一个工厂增加显式 keyword-only 依赖，不得另建模块全局或第二套应用工厂；最终装配顺序固定为 scope resolver/factory 及一致性校验、operation policy 与 callback verifier/issuer、路由服务、最后启动后台 Worker。缺失 resolver 时构造或 lifespan 启动 fail-closed，绝不把 None 解释为 local；其它安全依赖由各 capability 按启用状态 fail-closed。只有开源模块级入口显式传入 local resolver、allow-all policy 和安全 callback 适配器，以保留 uvicorn、测试夹具和现有前端入口。lifespan 创建一次 DatabaseResources、设置和重型 client，将显式依赖放入 app state，并只在全部装配校验通过后启动 Worker。公共请求开始时解析一次 scope，写入 request state；路由只读取该不可变上下文。

选择显式应用工厂而不是模块全局“当前 scope”，因为全局可变状态会在异步请求和 Worker 间串扰。选择协议注入而不是公共核心认证，是因为适配宿主需要替换解析来源而不应把用户实体带入同步点。

### 2. 共享重资源，按 scope 创建不可变服务环境

ScopeServiceFactory 只保存 DatabaseResources、设置、共享只读配置和可安全复用的 HTTP/model client。for_scope(scope) 返回不可变的 ScopeServices 视图，为 metadata、search、visual、reverse-image、collection 和 task 操作创建绑定该 scope 的 service/repository；scope-bound 对象不写回 factory，也不允许设置新的 scope。

DatabaseResources.environment(scope_id) 继续作为数据库 Unit of Work 的唯一入口；blob_store_for_scope 返回对应的 BlobStore。local 继续映射当前 image_root，其他 scope 使用不可由客户端决定的 namespace。每个请求或任务结束时关闭 Session 和临时 service，连接池和 client 不随 scope 复制。

每 scope 创建独立应用或线程池会线性复制重资源；用 contextvars 隐藏 scope 容易让后台任务继承错误上下文。因此使用显式 factory 和不可变上下文。

factory 是适配宿主可注入的适配边界，不能只依赖其实现自觉保持一致。请求 middleware、`services_for_task`、内部 `for_task` callback 和 Worker claim 后的 service resolver 都必须调用同一个 `validate_scope_services(expected_scope, services)` 校验。校验至少覆盖 `ScopeServices.scope` 及 metadata、search、tasks、reverse-image、visual-search 等 scope-bound 子服务；任何不一致立即 fail-closed。

### 3. 公共请求与后台任务使用不同的 scope 事实来源

公共请求在中间件或统一依赖中调用 resolver，将 ScopeContext 放到 request.state.scope。图片、搜索、合集和公共任务 API 从 request scope 操作资源；客户端无法通过字段或路径切换 scope。

新建任务把 request scope 持久化到 Task.scope_id。Worker 认领、重试、子任务、heartbeat、fencing、finalizer 和业务写回从该列或有效 claim 恢复 scope。公共任务查询仍必须先按 request scope 过滤。

Agent、视觉和反向图片的内部 task callback 不依赖用户 request scope。它们由公共 `agent-internal-callbacks` capability 验证服务身份与当前 Task 执行绑定，再校验任务类型、状态和完整 claim，并以持久 `Task.scope_id` 创建服务环境；适配宿主只能替换 verifier，不能放宽核心 Task/scope 校验。本 change 本身不实现该认证机制。

公共资源 lookup 在 request scope-bound repository 中完成；跨 scope UUID 仍返回资源不存在或无权错误，不返回 scope/user 字段、物理 namespace 或绝对路径。

### 4. 将任务协调器改成 scope-aware，而不是每 scope 一个 Worker

保留一个进程级 TaskWorkerManager 管理线程池、handler 注册和全局 Agent lane 背压；请求侧使用 scope-bound task facade 提交、查询、取消和重试。提交 facade 从 request scope 写入 Task.scope_id。

Worker 认领任务行后创建 ScopeServices.for_scope(ScopeContext(task.scope_id)) 并调用 handler。handler 不捕获请求级 singleton；所有写回以 claim 的 scope、owner、lease 和 generation fencing，禁止读取 Worker 默认 local。

跨 scope 扫描由协调层执行安全查询，每一行在认领事务中校验 scope。现有 lane slot、claim generation 和 PostgreSQL 唯一约束继续承担多进程互斥，不建立每 scope 的内存队列。无效 scope 的任务进入稳定配置错误，不猜测为 local。

Worker 的 scope 装配失败必须保留 claim fencing。装配异常处理函数接收完整 claim（task id、scope id、owner、claim generation），并以这些条件和有效租约执行条件更新；不能只按 task id 查询后把任意 `queued/running` 任务标记为失败。lane slot 只有在同一条件更新成功后才可释放；影响行数为零时只记录 fencing rejection。

视觉子任务、batch item、finalizer、反向图片 usage event 和 provenance 都在父任务 scope 环境中写入。相关 active changes 保持它们对审计、缓存和 executor 协议的所有权。

### 5. 相关 active changes 通过同一 scope 机制集成

add-task-scoped-reverse-image-search 保留其反向图片策略、usage event 与缓存语义；本 change 只让其服务从任务 scope 创建环境。share-and-import-meme-collections 保留 ZIP 格式和部分成功语义；本 change 只替换固定 local 的服务装配。2026-08-15-compose-agent-executor 保留 executor token 和固定 HTTP 协议；Runner 以任务 scope 生成受控图片输入，但不新增 scope 字段。

三者的实施顺序是：先建立 resolver、factory 和任务 scope 传播，再分别接入路由、Worker 和既有 active changes 的集成测试。宿主 non-local Agent 图片挂载由宿主提供 scope-aware input provider；未配置时任务必须稳定失败，不能把范围扩大到可读其他 scope 的文件。

## Risks / Trade-offs

- [Risk] 遗漏一个 environment("local") 或固定 service 引用会形成跨 scope 读写。→ 静态扫描生产路径，并用双 scope 并发集成测试覆盖路由和 Worker。
- [Risk] scope-bound service 增加对象开销。→ 只创建轻量 facade/Session，重用应用级 Engine、连接池和 client。
- [Risk] 旧 Worker 在重新认领后写回。→ 所有副作用都以 task scope 和 claim generation fencing 执行。
- [Risk] 自定义 factory 返回错误 scope 的服务或装配异常晚到。→ 所有服务装配边界执行 scope 一致性校验；装配失败使用完整 claim fencing，不能按裸 task id 终止任务或释放 slot。
- [Risk] scope-aware 路由先上线而 callback 仍沿用内网信任。→ 6.1-6.4 是强制启用 callback 和归档反向图片 capability 的硬门槛；随后必须应用 `secure-agent-internal-callbacks`，相关接口的 scope 始终以 `Task.scope_id` 为唯一事实来源。
- [Risk] 旧应用回滚无法理解 non-local 任务。→ 回滚前关闭 non-local 流量并收束其活动任务；保留数据和 namespace，不映射到 local。

## Migration Plan

1. 新增 resolver、显式应用工厂、request scope state 和 service factory；开源模块级入口显式传入 local resolver。
2. 将公共路由和服务迁移到 scope-bound facade，静态扫描移除生产路径中的隐式 local。
3. 将任务提交和 Worker 迁移到 Task.scope_id，验证重试、重启、heartbeat、fencing、子任务和 finalizer。
4. 完成任务 6.1-6.4 的统一 service scope 校验、完整 claim fencing、lane slot 绑定和并发回归；这是 operation/callback 强制启用、图片 Worker 切换和反向图片归档的硬门槛。
5. 在同一个 `create_app` keyword-only 装配边界接入 operation policy、callback verifier/issuer 和图片 Worker，再切换反向图片、合集和 executor active changes，完成 local 回归和双 scope 并发验收。
6. 宿主 staging 注入真实 resolver、operation policy、callback verifier 与受控 Agent input provider；回滚时先关闭 non-local 流量并诊断性收束 non-local 任务，不删除其数据。

## Open Questions

无。宿主 resolver、callback verifier 和 non-local Agent 图片挂载由宿主部署注入；公共核心仍拥有 Task/claim/scope 复核，不改变本 change 中 scope 的事实来源和隔离边界。
