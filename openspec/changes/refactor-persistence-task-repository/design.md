## Context

前置切片已将 ORM 模型、engine/UoW、资源装配以及 Meme、合集、文本搜索和视觉向量 Repository 移入 `backend.persistence`，但 `backend/database.py` 仍实现 Task、ReverseImage usage 和 callback 事实。`DataEnvironment` 通过延迟 facade 导入把这些对象绑定到同一个 Session 和 scope。本 change 只改变实现归属，不改变事务、schema 或运行时编排。

## Goals / Non-Goals

**Goals:**

- 让任务持久化域的三个职责各有唯一实现来源：任务队列与批次、反向图片 usage、callback request 事实。
- 保留原方法顺序、SQL 条件、scope 过滤、状态转换、claim generation/owner/lease fencing、lane 公平性、幂等冲突和稳定分页语义。
- 保留 `backend.database`、`backend.persistence.repositories`、`DataEnvironment` 的兼容导出、属性名称和对象身份。
- 通过静态和运行时契约测试锁定新模块不顶层依赖 facade，并覆盖 fail-closed 错误、回归和 PostgreSQL-only callback schema 门禁。

**Non-Goals:**

- 不修改 Task、batch、lane、usage、callback ORM 模型、字段、索引、约束、Alembic migration 或数据库初始化。
- 不移动 `BlobStore`、`StorageCoordinator`、其它 Repository、HTTP、Worker、provider、callback token 验证、图片处理运行时或 frontend。
- 不新增任务状态、错误码、分页格式、fallback、内存生产实现或 SQLite 模拟 PostgreSQL/pgvector 行为。

## Decisions

### 1. 按持久化事实拆成三个 repository 模块

TaskRepository 放入 `tasks.py`，ReverseImageUsageRepository 放入 `reverse_image.py`，数据库和内存 callback repository 放入 `callbacks.py`。这些职责均是 scope-bound 的数据库事实且被同一 `DataEnvironment` 组合；分成三个文件能保持边界清晰，避免将 token、provider 或 Worker 运行时带入持久化模块。只移动 TaskRepository 会留下紧耦合 callback/usage 的零碎后续切片，因此本 change 一并收束同一任务事实域。

### 2. 保留 facade 作为唯一兼容层

`backend.database` 删除三个 class/夹具实现，仅导入新模块对象；Repository 包入口同时导出 canonical names。`DataEnvironment` 继续在构造时延迟从 facade 解析，以避免 `backend.persistence` 顶层导入 `backend.database` 形成循环，并保证一个 UoW Session 共享给所有 repository。

### 3. 任务成功写回通过局部 facade 依赖完成 usage 审计

`TaskRepository.complete_fenced_with_provenance` 仍需调用 ReverseImage usage 聚合，但任务模块不能顶层依赖 facade。保留原语义，在方法执行时局部解析 canonical `reverse_image` 模块；模块导入无反向 facade 依赖，数据库模块完成加载后该路径仍指向同一 ReverseImageUsageRepository 对象。

### 4. Callback 事实保持双索引和 fail-closed

PostgreSQL callback repository 继续先校验完整绑定，再确认逻辑唯一索引/非空列，按 request ID、逻辑键、确定性 ID 顺序解析并在唯一冲突后重新读取权威行。内存夹具只用于单元测试并复制双索引/终态收束语义，不作为生产 fallback。ReverseImage usage 继续以完整 binding 拒绝改绑，已完成事实只幂等返回。

### 5. 只以真实 PostgreSQL 作为数据库门禁

目标测试覆盖可静态/内存验证的安全边界；PostgreSQL 集成和 Compose 事实分别显式记录。未设置 `MEMEMEOW_DATABASE_URL` 或未启动 Compose 时只记录 skip，不用 SQLite 宣称真实任务并发、pg advisory lock、唯一约束或 callback schema 已验证。

## Risks / Trade-offs

- [移动大类时遗漏隐式模型或 helper 依赖] -> 对照原 class 逐方法迁移，运行 import、compileall、facade identity、资源装配和完整回归。
- [任务模块局部导入 ReverseImage 形成运行时循环] -> 只允许方法体内延迟解析，并用 AST 契约禁止新模块顶层导入 `backend.database`。
- [scope/claim/lease 条件被重写] -> 保留 SQL 和方法体，仅调整 import；增加跨 scope、旧 claim、过期 lease、slot fencing、状态和分页契约。
- [callback 迁移或唯一索引不可用时误放行] -> 保留 PostgreSQL schema 检查和稳定错误 `callback_binding_schema_unavailable`，SQLite 测试必须 fail-closed。
- [Server 脏文件被同步覆盖] -> merge 前检查目标路径没有重叠改动，只从开源精确 SHA fetch；其它脏文件不暂存、不提交、不清理。

## Migration Plan

1. 在开源仓库建立本 change artifacts，新增三个 canonical repository 模块、facade/package 导出和任务域契约/回归测试。
2. 运行开源目标测试、完整回归、PostgreSQL marker、OpenSpec strict、compileall 和 `git diff --check`，把实现、测试、artifacts、validation 和开源收尾合为一个提交。
3. 检查 Server 工作区脏文件与目标路径无重叠，从本地开源仓库精确 fetch 单一 SHA；使用一次 `git merge --no-ff --no-commit`，补入必要的 Server validation、OpenSpec 任务收束和 `docs/refactor-plan.md` 后只创建一个 merge commit。
4. 运行 Server 定向/完整回归和静态门禁，记录 merge SHA、双父提交、祖先关系、未配置 PostgreSQL/未运行 Compose 事实和回滚方式；不访问 upstream、不 push。

## Rollback

恢复 `backend/database.py` 中三组原实现并删除新增 repository 模块、包导出、契约测试和本 change artifacts；不回滚 ORM schema、migration、任务数据、usage 事件或 callback 事实。
