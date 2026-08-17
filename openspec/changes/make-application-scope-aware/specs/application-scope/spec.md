## Purpose

为公共核心定义可显式装配的可信 scope 边界，使开源 local 部署和宿主多 scope 宿主复用同一业务核心，并在并发请求和后台任务中保持资源隔离。

## ADDED Requirements

### Requirement: 应用必须显式装配可信 scope 解析器

系统 MUST 提供可注入的 ScopeResolver 和统一 resolve_scope(request) 入口。应用工厂 MUST 要求显式传入 resolver；resolver 缺失、无效或不能返回已验证的非空 scope 时，系统 MUST 在构造、启动或业务边界 fail-closed，且不得静默安装或回退到 local。只有开源模块级入口 MUST 显式使用 LocalScopeResolver("local")。客户端提交的 scope_id、user_id、路径前缀或普通请求字段 MUST NOT 改变解析结果。

#### Scenario: 开源模块级入口显式安装 local scope

- **WHEN** 开源模块级入口以 LocalScopeResolver("local") 创建应用并处理业务请求
- **THEN** resolve_scope(request) 返回 ScopeContext("local")
- **AND** 现有请求和响应契约保持不变

#### Scenario: 应用工厂缺失 resolver

- **WHEN** 宿主或其他宿主调用应用工厂但没有传入 ScopeResolver
- **THEN** 应用在构造、启动或业务边界失败
- **AND** 不创建把请求绑定到 local 的隐式 fallback

#### Scenario: 适配宿主解析受信任 scope

- **WHEN** 宿主适配层注入从可信请求上下文返回 scope-a 的 resolver
- **THEN** 公共业务请求使用 scope-a 读取和写入资源
- **AND** 同一请求中的普通字段不能切换到另一个 scope

#### Scenario: 客户端伪造 scope 字段

- **WHEN** 客户端提交 scope_id、user_id 或包含其他 scope 前缀的资源参数
- **THEN** 系统拒绝该字段或忽略其覆盖意图
- **AND** 不访问或泄露被伪造 scope 的资源

### Requirement: scope-bound 服务环境必须在并发请求间隔离

系统 MUST 将 scope 绑定到请求级或任务级的服务环境。共享的 Engine、连接池、只读模型和 HTTP client 可以复用，但任何共享的 scope-bound service、repository、缓存状态或文件根 MUST NOT 在请求之间原地切换 scope。并发请求 MUST 能同时使用不同 scope 而互不污染。

#### Scenario: 并发请求使用不同 scope

- **WHEN** scope-a 和 scope-b 的请求同时执行列表、搜索或媒体操作
- **THEN** 每个请求只读取其解析 scope 的服务环境
- **AND** 请求完成后另一个请求的 scope、缓存或文件根不发生改变

#### Scenario: scope-bound 对象生命周期结束

- **WHEN** 一个请求或任务完成、失败或被取消
- **THEN** 其 Session、scope-bound repository 和临时 provider 被释放或失效
- **AND** 后续请求不会复用带有旧 scope 的可变业务状态

### Requirement: 请求和任务必须使用各自可信的 scope 事实来源

系统 MUST 让上传、媒体、元数据、搜索、合集以及公共任务提交和查询入口使用 resolve_scope(request)。异步 Worker、子任务、重试和可信内部 task callback MUST 从持久 Task.scope_id 或有效 claim 得到 scope；它们不得依赖用户 request scope、客户端字段、普通 payload 或进程默认值。内部 callback 的认证或服务间凭据边界由相关 change 和适配宿主负责，本 change 不新增或声称存在该机制。任何入口不得保留因调用路径不同而出现的隐式 local fallback。跨 scope 资源标识 MUST 按未知资源或无权访问处理，且不得泄露存在性。

#### Scenario: 公共任务查询跨 scope

- **WHEN** scope-a 通过公共 API 查询仅属于 scope-b 的 task ID
- **THEN** 系统先按 resolve_scope(request) 过滤并返回未知任务的稳定错误
- **AND** 响应不表明 scope-b 的任务是否存在

#### Scenario: 后台 task callback 从持久任务取得 scope

- **WHEN** Agent、视觉或反向图片的可信 task callback 处理一个 task ID
- **THEN** 系统在其既有服务间信任边界内校验任务类型、状态和适用 claim 后，从持久 Task.scope_id 构造 scope-bound 环境
- **AND** callback 字段不能覆盖 scope、查询 Meme 或文件路径

#### Scenario: 跨 scope 标识访问

- **WHEN** 公共请求使用属于另一 scope 的 meme_id 或 collection ID，或内部 callback 使用不属于有效 claim 的 task ID
- **THEN** 系统返回与未知资源或无效 callback 相同的稳定错误
- **AND** 响应、状态码时序和日志对外不表明目标资源是否存在

### Requirement: 后台任务必须持久化并传播可信 scope

系统 MUST 在创建异步任务时保存可信请求 scope 到不可为空的 Task.scope_id，并 MUST 在 Worker 认领、视觉任务、Agent 子任务、批次 finalizer、重试、heartbeat、fencing、反向图片检索和终态写回时从持久任务记录或有效 claim 读取该 scope。普通 payload、客户端字段和 Worker 当前默认 scope MUST NOT 作为后台业务归属来源。

#### Scenario: 请求创建任务后 Worker 重启

- **WHEN** scope-a 请求创建的任务在 Worker 重启后被重新认领
- **THEN** 新 Worker 按持久 Task.scope_id=scope-a 读取图片并写回结果
- **AND** 不因 Worker 默认配置为 local 而漂移

#### Scenario: 旧 claim 尝试跨 scope 写回

- **WHEN** 旧 Worker 的 claim 失效且任务已由另一 Worker 重新认领
- **THEN** heartbeat、进度、终态和业务副作用写回均被 fencing 拒绝
- **AND** 不修改当前有效 claim 所属 scope 的资源

### Requirement: scope service factory 返回值必须与期望 scope 一致
应用边界、后台任务 resolver 和内部 callback MUST 在使用 factory 返回的 `ScopeServices` 前验证其外层 scope 与期望 scope 相等，并 MUST 验证其中的 scope-bound 子服务不会绑定到另一个 scope。验证失败 MUST fail-closed，不得继续执行数据库、文件、搜索、任务或 Agent 操作，也不得回退到 local。

#### Scenario: 请求 scope 与 factory 服务 scope 不一致
- **WHEN** resolver 返回 scope-a，但 factory 返回绑定 scope-b 的 `ScopeServices`
- **THEN** 请求以稳定的 scope 不可用错误终止
- **AND** 不读取、写入或泄露 scope-a 或 scope-b 的业务资源

#### Scenario: 后台 task scope 与 factory 服务 scope 不一致
- **WHEN** 持久 Task.scope_id 为 scope-a，但后台 resolver 返回绑定 scope-b 的服务
- **THEN** Worker 在调用 handler 前拒绝该 claim 的业务执行
- **AND** 不让该服务提交进度、终态或业务副作用

### Requirement: scope 装配失败不得终止其他 Worker 的有效 claim
Worker 在 claim 后装配 scope 服务失败时，失败收束 MUST 使用该次 claim 的完整 scope、lease owner 和 claim generation 条件。条件更新影响行数为零时 MUST 视为 claim 已失效或已被其他 Worker 接管，不得修改任务终态、释放其他 Worker 的 lane slot 或覆盖其诊断。

#### Scenario: 旧 Worker 的装配异常晚于重新认领
- **WHEN** Worker A 持有任务的旧 claim，装配服务失败且其租约过期，Worker B 已用更高 claim generation 重新认领
- **THEN** Worker A 的失败收束被 fencing 拒绝
- **AND** Worker B 的任务状态、租约、lane slot 和后续写回保持不变

#### Scenario: 当前 claim 装配失败
- **WHEN** Worker 在自己的有效 claim 内确认 scope factory 不可用
- **THEN** 系统仅收束该 owner 和 generation 对应的任务，释放属于该 claim 的 lane slot，并记录稳定配置错误
