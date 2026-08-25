## Context

上一切片已把 ORM 模型移动到 `backend.persistence.models`，并验证
`backend.database` 的显式模型 re-export。当前 `database.py` 的 engine 函数位于文件
开头，`UnitOfWork` 紧随其后，`DataEnvironment` 位于 Repository 与 BlobStore 之间，
`DatabaseResources` 位于文件末尾。资源类需要组装尚未拆出的 Repository、BlobStore 和
StorageCoordinator，因此本切片必须保留单向运行时依赖，不能制造第二套 repository 或
storage 实现。

## Goals / Non-Goals

**Goals:**

- 让 `backend.persistence.engine` 成为数据库连接、启动 schema 兼容和 local 初始化的
  唯一实现来源。
- 让 `backend.persistence.unit_of_work` 成为同步 `UnitOfWork` 的唯一实现来源。
- 让 `backend.persistence.resources` 成为 `DataEnvironment` 与 `DatabaseResources` 的
  唯一实现来源，并继续组装现有 Repository、BlobStore、StorageCoordinator。
- 让 `backend.database` 继续导出旧符号，且 facade 与新模块返回同一类/函数对象。
- 通过契约测试和静态检查锁定依赖方向、metadata、schema/migration 和资源生命周期事实。

**Non-Goals:**

- 不拆分任何 Repository、BlobStore、StorageCoordinator 或业务服务。
- 不修改 schema、Alembic migration、SQL 语句、表/列/约束/索引、事务提交/回滚语义、
  BlobStore 路径语义或 scope 权限语义。
- 不迁移现有业务模块的导入路径，不引入新的 HTTP 或服务层。

## Decisions

### 1. Engine 独立依赖模型

`engine.py` 只依赖 SQLAlchemy、项目模型和稳定基础值。`DatabaseError` 随 engine 一起
移动，避免 engine 反向依赖兼容 facade；`database.py` 和 resources 重新导出同一错误类。
可选控制面 SQL 与 `Base.metadata` 原样移动，不复制 metadata 或迁移事实。

### 2. UnitOfWork 只依赖 ScopeContext

`UnitOfWork` 只接收已有 `sessionmaker` 和 `ScopeContext`，保留成功提交、异常回滚、最终
关闭 Session 及显式 rollback 行为。它不导入 facade、Repository 或资源模块。

### 3. Resources 延迟组装运行时组件

`resources.py` 直接依赖 engine、UoW、models 和路径/Session 基础类型；在创建
`DataEnvironment`、`BlobStore` 或 flat preflight 时，使用函数内导入已留在
`database.py` 的 Repository、BlobStore、StorageCoordinator。这样资源实现成为独立边界，
同时避免 database facade 导入 resources 时的循环依赖和第二套实现。

### 4. Facade 显式导出

`backend.database` 删除指定实现，仅显式导入并 re-export 新模块的函数、错误类和类对象。
现有业务模块和 Alembic 继续使用旧路径；契约测试直接比较对象身份并检查新模块不反向导入
`backend.database`。

## Risks / Trade-offs

- [facade 漏导旧符号] -> 测试旧函数/类身份和所有指定 engine 函数，同时运行目标与全量回归。
- [资源模块发生导入循环] -> 静态检查其顶层导入；Repository/BlobStore/StorageCoordinator
  只在调用点延迟导入，并运行 import/compileall 门禁。
- [移动代码时遗漏行为] -> 逐段保留原方法体与 SQL，测试 UoW 生命周期、scope 资源、
  optional schema、数据库检查及 local 初始化；不生成新 migration。
- [回滚顺序错误] -> 先恢复 `backend/database.py` 对应实现，再删除新模块和契约测试；
  schema/migration 不需要回滚。

## Migration Plan

1. 在开源仓库新增模块、facade 导出、契约测试和 OpenSpec artifacts，运行目标测试、相关
   全量回归、strict validate、compileall 和 `git diff --check`；记录 PostgreSQL/Compose
   门禁是否实际运行。
2. 在开源仓库依次提交实现、验证与收尾记录，固定精确 SHA、变更范围、回滚方式和祖先关系。
3. 检查 Server 工作区，若目标文件存在重叠脏改动则停止；否则从本地开源仓库精确 fetch
   收尾 SHA，并在 Server `main` 使用普通 `--no-ff` merge。
4. 在 Server 运行定向/全量回归和静态门禁，记录 merge SHA、开源 SHA 祖先关系、未运行的
   PostgreSQL/Compose 门禁及回滚方式。
