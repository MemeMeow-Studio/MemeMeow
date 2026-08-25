## Why

`backend/pg_services.py` 同时实现元数据文件生命周期、向量检索、任务执行器和进程级
Worker 协调器，约两千行代码共享导入、日志和兼容入口。高密度 facade 让 scope 绑定、
事务边界、任务 claim/lease fencing 和失败恢复难以单独审查，也使后续应用服务演进必须
修改一个巨型模块。

## What Changes

- 新增 `backend/services/metadata.py`，承载 `PostgresMetadataService` 的 scope-bound
  元数据、图片身份、文件生命周期和 provenance 编排。
- 新增 `backend/services/search.py`，承载 `PostgresSearchService` 的 embedding、缓存和
  当前 scope 检索编排，并只依赖 metadata 服务的显式接口。
- 新增 `backend/services/worker_manager.py`，承载 `PostgresTaskWorkerManager` 的进程级
  线程池、handler registry、跨 scope 恢复、公平 claim 和任务调度协调。
- 新增 `backend/services/tasks.py`，承载 `PostgresTaskService` 的任务提交、去重、批次、
  claim/lease、执行、错误/恢复、审计和分页编排，并只依赖 worker manager 的回调边界。
- 让 `backend/pg_services.py` 只保留四个 canonical service 的显式 re-export，旧 import
  路径、对象身份、构造参数和 monkeypatch 入口继续可用。
- 增加模块身份、依赖方向、scope/权限、事务、任务状态和兼容 API 契约测试；实现、测试、
  OpenSpec artifacts、validation 和同步记录保持在同一职责域提交链。

## Capabilities

### New Capabilities

- `service-facades-domain`: PostgreSQL 应用服务 facade 的模块边界、scope 绑定、兼容导出
  和任务执行语义。

### Modified Capabilities

无。本 change 只移动既有实现，不新增公开协议或状态。

## Impact

影响公共核心 `backend/pg_services.py`、新增 `backend/services` 包、scope 工厂的 lazy
import 解析以及 facade/任务回归契约测试。数据库 ORM、migration、HTTP route、DTO schema、
账户/quota/security、image processing、OpenCode/executor、frontend 均不属于本 change。
服务模块继续通过现有 Repository、StorageCoordinator、operation policy 和运行时 handler
边界工作，不新增依赖。
