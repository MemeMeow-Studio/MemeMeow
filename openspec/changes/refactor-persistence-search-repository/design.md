## Context

上一阶段已经把 ORM models、engine、UnitOfWork、resources 以及 Meme/Collection Repository 移到 `backend.persistence`，但 SearchRepository 仍位于 `backend/database.py` 前部。它只需要 SQLAlchemy 查询、搜索模型、Meme 模型、任务 claim 校验和 metadata hash 计算，却被旧 facade 的其它职责包围，增加了审查面。

## Goals / Non-Goals

**Goals:**

- 让 `backend/persistence/repositories/search.py` 成为 SearchRepository 的唯一实现来源。
- 保留 generation/head、migration state、legacy/incremental text embedding、metadata/SHA/revision 校验、余弦查询和稳定排序的原方法体及行为。
- 让 `backend.database.SearchRepository` 与新模块导出同一类对象，并保持 `DataEnvironment` 现有 lazy facade assembly、单 Session 和 scope 绑定。
- 通过静态实现唯一性、facade identity、资源装配、migration source dispatch 和查询错误/排序回归锁定边界。

**Non-Goals:**

- 不迁移或修改 VisualEmbeddingRepository、TaskRepository、ReverseImageUsageRepository、AgentCallbackRequestRepository、BlobStore、StorageCoordinator、DatabaseResources、UnitOfWork、engine、ORM models、schema、Alembic migration、HTTP、frontend 或任务服务。
- 不改变任何 SQL、表/列/索引/约束、事务提交/回滚、scope 权限、错误码、查询 limit/排序、向量维度或 migration 控制事实。
- 不引入第二套 search service、向量实现、缓存或抽象层。

## Decisions

### 1. 一个 Repository 一个模块

直接从 `backend/database.py` 原位置移动 SearchRepository 类体到 `repositories/search.py`，按实际引用保留最小 import。方法顺序和方法体保持不变；只调整模块级依赖和中文模块/class docstring，使审查可以对照原实现。

### 2. 新模块只依赖持久化基础层

新模块直接导入 `backend.persistence.engine.DatabaseError`、`backend.persistence.models`、路径校验和 SQLAlchemy 基础类型；不顶层导入 `backend.database`。`backend.metadata` 仍在 metadata hash/源快照计算方法内按原方式延迟导入，避免领域循环。

### 3. Facade 显式 re-export，资源装配不同时迁移

`backend.database` 删除 SearchRepository class 定义，显式导入新类。`backend.persistence.resources.DataEnvironment` 保持既有函数内 `from backend.database import SearchRepository`，因此资源装配、scope 绑定和单一 UnitOfWork Session 不变；不在本 change 重排其它 Repository。

### 4. 契约测试覆盖高风险边界

新增/扩展持久化边界测试直接比较旧/新类身份，解析 AST 确认新模块没有 facade 顶层导入且旧文件不再含 SearchRepository class。保留现有 SearchRepository.query 单来源测试，并增加 source dispatch/排序/错误和迁移状态回归；PostgreSQL marker 作为环境门禁运行，未配置连接串时明确记录 skip。

## Risks / Trade-offs

- [移动时遗漏模型或 SQL import] -> 从类体逐项核对实际符号，运行 import、compileall、目标测试和完整回归。
- [旧 facade 漏导或循环导入] -> facade identity/AST 契约和 DataEnvironment 共享 Session 测试；新模块禁止顶层 facade import。
- [无意改变查询行为] -> 类体逐段对照，覆盖 migration source exclusivity、无效行过滤、limit、排序和错误码；不对 SQL 做重写。
- [环境门禁缺失] -> 运行 PostgreSQL marker；没有连接串时明确报告未连接数据库，不以 SQLite 冒充 pgvector 验收。

## Migration Plan

1. 在开源仓库补齐本 change artifacts，新增 search Repository 模块、facade 导出、契约/回归测试和 validation 记录；运行目标测试、完整回归、strict validate、compileall 与 `git diff --check`。
2. 在开源仓库提交实现、验证和收尾 artifacts，固定精确收尾 SHA；不访问 upstream、不 push。
3. 检查 Server 工作区目标路径没有重叠脏改动，从本地开源仓库精确 fetch 收尾 SHA，并在 Server `main` 使用普通 `--no-ff` merge；不得复制实现或 cherry-pick。
4. 在 Server 更新本 change validation 与 `docs/refactor-plan.md`，记录开源实现/验证/收尾 SHA、Server merge SHA、直接父提交和祖先关系、测试结果、未配置 PostgreSQL 事实、未运行 Compose 门禁及回滚方式。

## Rollback

先恢复 `backend/database.py` 中原 SearchRepository class 实现并删除 facade import，再删除 `backend/persistence/repositories/search.py`、包导出、契约/回归测试和本 change artifacts；不回滚 schema、migration 或业务数据。
