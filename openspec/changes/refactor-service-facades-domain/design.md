## Context

前置持久化切片已经把 models、engine/UoW、resources、Meme/collection、Search、视觉向量、
Task 和 storage 实现移入 `backend.persistence`，但 `backend/pg_services.py` 仍把四个应用
服务职责放在一个模块。`ScopeServiceFactory`、API 装配、图片处理和测试长期从旧路径导入
这些类，因此本 change 必须先形成 canonical service 模块，再以纯 facade 兼容旧入口。

## Goals / Non-Goals

**Goals:**

- 为 metadata、search、task 和 worker-manager 建立四个单一实现来源和明确依赖方向。
- 保留所有公开类名、构造参数、方法返回 DTO、scope 绑定、权限/grant 调用、事务边界、
  错误码、任务状态、lease/claim fencing、批次收束、审计和恢复语义。
- 保留 `backend.pg_services` 的旧导入、对象身份和现有 monkeypatch 入口；`ScopeServiceFactory`
  继续按同一 scope 组装并验证 services。
- 通过静态边界测试、兼容导出测试、目标服务回归、完整回归和 strict validation 证明移动
  没有产生第二套实现。

**Non-Goals:**

- 不修改 ORM 模型、数据库 schema/migration、Repository SQL、StorageCoordinator 或数据。
- 不改变 HTTP route、公开 DTO、错误投影、scope resolver、账户/quota/security、权限规则。
- 不移动 image_processing、OpenCode/executor、反向图片 provider、visual service、frontend
  或其它 active change；Worker manager 只保留现有任务调度协调，不接管这些运行时实现。
- 不引入异步框架、依赖注入容器或新的 fallback；canonical 模块不得反向导入 `pg_services`
  facade。

## Decisions

### 1. 服务模块按应用职责拆分

`metadata.py` 只处理 Meme 记录和受控 BlobStore/StorageCoordinator 编排；`search.py` 只处理
embedding/cache/query，并通过构造参数接收 metadata service；`worker_manager.py` 只处理
进程级调度、线程池、handler registry 和跨 scope claim/recovery；`tasks.py` 只处理 scope-bound
任务应用服务，依赖 worker manager 的显式 resolver/finish 回调。四个模块都可以依赖
`backend.persistence` 与既有 domain helper，但不依赖 HTTP 或 `backend.pg_services`。

### 2. 兼容 facade 只显式 re-export

`backend.pg_services` 删除 service class 实现和业务 helper，只导入并导出四个 canonical class。
旧调用方拿到同一 Python class 对象；新模块的日志使用稳定 `backend.pg_services` logger 名称，
避免改变既有运营日志筛选。scope 工厂继续局部导入旧 facade，因而无需修改其公共装配协议。

### 3. Worker manager 与 task service 单向协作

Worker manager 不导入 TaskService，只保存 task/scope resolver 回调，并在任务结束时调用既有
内部 finish/schedule 接口；TaskService 可以导入 manager 类型用于构造和注册，但 manager 不
反向导入 task module。任务 handler 仍由 API/宿主注册，外部执行和图片处理不进入本 change。

### 4. 以原实现为迁移基线

除 import、模块 logger 和必要的类型注解外，按原类边界逐段移动方法，不重写 SQL、事务块、
状态判断或错误映射。Server 专属 policy/quota 差异在同步阶段按原适配层代码解决，不能在
公共核心先行实现账户或 quota 逻辑。

## Dependency Direction

```text
HTTP / scope factory / scripts
              |
        pg_services facade (re-export only)
              |
  metadata <- search       tasks -> worker_manager
      |         |              ^          |
      +---------+--------------+----------+
          persistence / domain helpers / handler callbacks
```

Canonical service 模块只能向下依赖 persistence、domain helper 和标准库；它们不得导入
`api.py`、`server_api.py`、`backend.pg_services` 或 frontend。`search` 对 metadata 是单向
依赖；`worker_manager` 对 task service 只通过 Callable/Any resolver 协作，避免循环导入。

## Risks / Trade-offs

- [facade 漏导或重复实现] -> 显式 identity/AST/class-count 契约、compileall 和全量回归。
- [移动导致 Server quota 差异丢失] -> 先以开源精确 SHA 形成公共 canonical 模块，Server
  merge 时只解决对应模块的已有适配差异，并运行 Server quota/agent 回归。
- [scope/lease/事务语义被无意重写] -> 保留原方法体和 SQL；增加 scope factory、任务状态、
  claim fencing、失败恢复和权限/grant 目标测试。
- [日志来源变化影响安全审计] -> canonical 模块显式使用 `logging.getLogger("backend.pg_services")`。
- [Server 未提交文件被覆盖] -> merge 前只检查本 change 目标路径重叠；不暂存、stash、删除
  或覆盖其它 dirty files。

## Migration Plan

1. 在开源仓库建立本域 artifacts，逐类迁移四个 service 模块、facade 和契约测试。
2. 运行开源定向/完整回归、OpenSpec strict、compileall、`git diff --check`，记录 PostgreSQL
   与 Compose 是否真实运行；实现、测试、artifacts、validation 和收尾合一提交。
3. 用户审核/授权后，从本地精确 fetch 开源收尾 SHA，在 Server `main` 以一次普通 `--no-ff`
   merge 引入；只解决既有 Server policy/quota 与公共模块的必要冲突。
4. Server merge 后运行定向/完整相关回归、strict、compileall、diff check，更新同一 merge
   commit 中的 validation、`docs/refactor-plan.md` 和同步事实；不访问 upstream、不 push。

## Rollback

恢复 `backend/pg_services.py` 中四个 class 实现并删除 `backend/services` 包和本域测试/
artifacts；不回滚 schema、migration、任务数据、文件数据或 policy/grant 事实。
