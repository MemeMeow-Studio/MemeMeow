## Purpose

为 PostgreSQL 应用服务建立按职责拆分的 canonical 模块边界，同时让历史 facade、scope
装配和任务执行协议继续提供相同的安全、权限、事务、错误和状态事实。

## ADDED Requirements

### Requirement: Service implementations have one canonical boundary

系统 MUST 只在 `backend.services.metadata`、`backend.services.search`、
`backend.services.tasks` 和 `backend.services.worker_manager` 提供对应服务实现；
`backend.pg_services` MUST 只显式导出同一对象。canonical 模块 MUST 不得导入
`backend.pg_services`、HTTP 入口或 Server 专属模块。

#### Scenario: Legacy and canonical imports share identity

- **WHEN** 调用方分别从 `backend.pg_services` 和对应 `backend.services` 模块导入服务类
- **THEN** 两条路径返回同一个 Python class 对象，且既有构造参数和方法仍可调用

#### Scenario: Dependency direction is acyclic

- **WHEN** 静态检查四个 service 模块和兼容 facade 的 import/class 定义
- **THEN** facade 不包含服务实现，metadata/search/tasks/worker-manager 不反向依赖 facade、HTTP 或 frontend，
  search 只向 metadata 依赖，worker-manager 不导入 task service

### Requirement: Scope and application contracts remain bound

服务 MUST 继续使用构造时的 `ScopeContext` 选择数据库、BlobStore 和任务事实；scope factory
返回的 metadata/search/tasks 服务 MUST 绑定同一 scope。客户端 payload、query 或普通字段
不得替换服务 scope；scope、权限、grant、事务和 stable error 仍由原边界执行。

#### Scenario: Factory services share one trusted scope

- **WHEN** `ScopeServiceFactory.for_scope(scope)` 创建服务视图
- **THEN** metadata/search/tasks 的 `scope` 与外层 `ScopeServices.scope` 完全一致，跨 scope
  任务或文件访问继续 fail-closed

#### Scenario: Missing or invalid policy does not bypass authorization

- **WHEN** 任务/图片处理路径需要 operation policy 或 grant，而适配器缺失、拒绝或状态不确定
- **THEN** 服务保留既有稳定错误/unknown 语义，不静默 allow、release 未证实的计量或执行外部副作用

### Requirement: Task state and worker coordination are preserved

TaskService 和 WorkerManager MUST 保留既有提交去重、批次、queued/running/retry/terminal
状态、claim generation/owner/lease、lane slot、公平恢复、错误历史、resume、reverse-image
审计和分页 DTO 语义。WorkerManager MUST 通过既有 resolver/handler 回调创建正确 scope
service，不从 payload 推导 scope。

#### Scenario: Claim fencing rejects stale execution

- **WHEN** 任务 claim 的 owner、generation、attempt 或 lease 不再匹配
- **THEN** 旧执行不能写回成功/失败终态、Meme provenance 或释放不属于它的 lane slot，且
  返回既有稳定状态/错误事实

#### Scenario: Dedupe and batch finalization remain idempotent

- **WHEN** 相同 task payload 重复提交，或批次成员并发完成/失败
- **THEN** 既有 dedupe key、活动任务复用、批次 finalizer 和任务 DTO 结果保持一致，不创建
  重复 durable task 或重复外部计量

### Requirement: Compatibility surface remains stable

`backend.pg_services` MUST 继续导出四个 service 类和调用方当前依赖的模块 import 路径；
scope factory、API、脚本、图片处理 Worker、测试 monkeypatch 无需迁移。返回的 `TaskRecord`、
`SidecarMetadata`、搜索 meme_id 列表、稳定错误码和日志/审计敏感信息脱敏语义 MUST 保持不变。

#### Scenario: Existing application imports continue to work

- **WHEN** API、scope factory、backfill script 或旧测试从 `backend.pg_services` 导入类
- **THEN** 导入成功并获得 canonical object，运行行为与拆分前一致

#### Scenario: Public errors remain stable and non-sensitive

- **WHEN** service 抛出路径、数据库、scope、policy、executor 或任务错误
- **THEN** 调用方只观察既有稳定错误码/DTO，物理路径、scope namespace、凭据和内部 traceback
  不进入公开结果
