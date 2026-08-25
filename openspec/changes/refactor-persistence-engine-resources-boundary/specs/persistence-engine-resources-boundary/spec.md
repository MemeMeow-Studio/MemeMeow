## Purpose

为连接、事务和 scope 资源装配提供单一、可审查且保持历史导入兼容的持久化边界，
在不改变数据库事实或业务协议的前提下继续降低 `backend.database` 的职责密度。

## ADDED Requirements

### Requirement: Engine functions have one implementation source

系统 MUST 只在 `backend.persistence.engine` 实现 `database_url_from_env`、
`create_engine_for_url`、`create_engine_for_settings`、`ensure_optional_control_schema`、
`check_database` 和 `initialize_local`；这些函数 MUST 保持原有参数、返回值、错误码、
SQL、事务和 schema/migration 语义。

#### Scenario: Engine facade preserves function identity

- **WHEN** 调用方分别从 `backend.persistence.engine` 和 `backend.database` 导入六个函数
- **THEN** 每个名称返回同一个 Python 函数对象，旧调用路径无需迁移

#### Scenario: Database checks keep deployment gates

- **WHEN** 调用数据库检查或 local 初始化
- **THEN** 连接、pgvector、Alembic revision、installation marker 和冲突错误仍按原规则
  校验，不以异常吞掉或静默降级代替原错误

### Requirement: Unit of Work has one transaction boundary

系统 MUST 只在 `backend.persistence.unit_of_work` 实现 `UnitOfWork`；成功退出必须提交，
异常退出必须回滚，任何退出路径都必须关闭 Session，显式 `rollback()` 必须保持可用。

#### Scenario: Unit of Work lifecycle remains stable

- **WHEN** 使用同一 `sessionmaker` 和 `ScopeContext` 进入/退出事务
- **THEN** repository 仍共享同一 Session，成功提交、异常回滚与关闭行为和旧实现一致

### Requirement: Resources keep scope-bound assembly facts

系统 MUST 只在 `backend.persistence.resources` 实现 `DataEnvironment` 和
`DatabaseResources`；二者 MUST 继续使用原 Session 工厂、scope 绑定、local scope 安装门禁、
BlobStore 命名空间和 flat preflight 事实。

#### Scenario: Data environment keeps one session and scope

- **WHEN** 通过 `DatabaseResources.environment(scope_id)` 创建环境
- **THEN** 所有现有 Repository 和 visual alias 共享同一 UnitOfWork Session，并拒绝缺失 scope

#### Scenario: Database resources keep storage lifecycle

- **WHEN** 构造或按 scope 请求 `DatabaseResources`
- **THEN** optional control schema、local installation 检查、local image root、非 local
  storage namespace、flat preflight 和 `scope_not_found` 错误与旧行为一致

### Requirement: Legacy facade and dependency direction remain compatible

`backend.database` MUST 显式 re-export新模块的 engine 函数、`DatabaseError`、`UnitOfWork`、
`DataEnvironment` 和 `DatabaseResources`；新模块 MUST 不在顶层导入 `backend.database`，
且不得新增 Repository、BlobStore、StorageCoordinator、schema 或 HTTP 实现。

#### Scenario: Existing imports continue to resolve

- **WHEN** Alembic、业务模块和测试从 `backend.database` 导入旧符号
- **THEN** 导入成功、对象身份与新模块一致，现有行为不要求调用方改路径

#### Scenario: New boundaries are independently importable

- **WHEN** 静态检查并单独导入 engine、unit_of_work 和 resources
- **THEN** 不存在反向 facade 导入循环，模块可 compile/import，Repository/存储代码仍只有
  `backend.database` 中的原实现

### Requirement: Database facts remain unchanged

该 change MUST NOT 修改 Alembic migration、metadata 表集合、表/列/约束/索引、Repository、
BlobStore、StorageCoordinator、HTTP、任务协议或文件存储事实。

#### Scenario: Persistence regression sees the same facts

- **WHEN** 运行持久化契约、相关 API/scope/task 测试和全量 Python 回归
- **THEN** 现有 schema/metadata、scope 隔离、任务/存储事务和 API 兼容测试继续通过
