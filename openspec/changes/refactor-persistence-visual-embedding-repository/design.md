## Context

前置持久化切片已将模型、engine/UoW、资源装配以及 Meme、合集、文本搜索 Repository 移入 `backend.persistence`，但视觉向量校验和 repository 仍位于 `backend/database.py`。该实现只需要模型层、数据库错误、SQLAlchemy Session 和 scope 上下文，调用方则通过 `DataEnvironment.visual` 使用同一个事务 Session。详见 proposal.md 与本 change spec。

## Goals / Non-Goals

**Goals:**

- 让 `backend/persistence/repositories/visual_embeddings.py` 成为视觉向量校验和 repository 的唯一实现来源。
- 保留 `get`、`upsert`、`agent_ready`、`match`、`query` 的 SQL、事务、scope、模型身份、SHA 校验、候选过滤、错误码和稳定排序语义。
- 保留 `backend.database.VisualEmbeddingRepository` 与 `validate_visual_vector` 的旧导出，并确保它们分别与新模块对象身份一致。
- 用静态和运行时契约测试锁定 facade、依赖方向、资源装配和视觉向量高风险边界。

**Non-Goals:**

- 不迁移或修改 `TaskRepository`、`ReverseImageUsageRepository`、`AgentCallbackRequestRepository`、`BlobStore`、`StorageCoordinator`、`DatabaseResources`、`UnitOfWork`、engine、ORM models、schema、Alembic migration、HTTP、视觉推理服务或 frontend。
- 不改变数据库表、列、约束、索引、pgvector 类型、事务提交/回滚、scope 权限、错误码、向量维度、模型身份或匹配结果排序。
- 不引入新的视觉服务、缓存、向量算法、fallback 或额外抽象层。

## Decisions

### 1. 一个 repository 一个模块

直接把 `backend/database.py` 中的 `validate_visual_vector` 与 `VisualEmbeddingRepository` 方法体移到 `repositories/visual_embeddings.py`，保留顺序和 SQL 表达式。这样校验函数和其唯一使用者不会被拆散，也避免为单个 repository 增加 service 层。

备选方案是只移动 class、把校验函数留在 facade；这会让 canonical repository 继续依赖兼容入口，并保留视觉持久化实现跨模块分裂，因此不采用。

### 2. 新模块只依赖持久化基础层

新模块直接导入 `backend.persistence.engine.DatabaseError`、`backend.persistence.models` 和 SQLAlchemy 基础类型，不在顶层导入 `backend.database`。模型层的 `Meme`、`MemeVisualEmbedding`、`StorageOperation`、`ScopeContext` 和 `utcnow` 是原 SQL 所需的最小依赖；领域视觉服务仍可从 facade 兼容导入。

### 3. Facade 显式 re-export，资源装配不迁移

`backend.database` 删除两个定义并导入新模块对象；`backend.persistence.repositories.__init__` 同时暴露 canonical names。`DataEnvironment` 仍在构造时从 facade 延迟解析 `VisualEmbeddingRepository`，所以 Session、scope 和 environment 属性不变，也不扩大本 change 的资源边界。

### 4. 契约测试覆盖 fail-closed 边界

测试直接比较旧/新类和函数身份，解析 AST 确认新模块没有 facade 顶层导入且旧文件不再含视觉 class/validator 定义；运行时回归覆盖向量维度、有限性、零范数、模型身份、SHA、scope、Agent provenance、活动存储操作过滤、排除自身和稳定排序。真实 PostgreSQL 只通过显式 marker 验收；未配置连接串时记录 skip，不以 SQLite 代替 pgvector 验收。

## Risks / Trade-offs

- [移动时遗漏模型或工具 import] -> 对照原类体逐项核对，运行 import、compileall、契约/视觉目标测试和完整回归。
- [旧 facade 导出遗漏或循环导入] -> facade identity、包入口 identity、AST 依赖和 DataEnvironment 共享 Session 测试。
- [向量安全语义被意外改写] -> 保留方法体和 SQL，仅调整模块依赖；覆盖非法输入、跨 scope、SHA 不匹配、非 Agent-ready 和活动存储操作过滤。
- [PostgreSQL/pgvector 门禁缺失] -> 运行 PostgreSQL marker；没有连接串时明确记录未连接，不宣称真实数据库验证。

## Migration Plan

1. 在开源仓库完成本 change artifacts、新 repository 模块、facade/package 导出、契约/回归测试和 validation 记录，运行目标测试、完整回归、strict validate、compileall 与 diff check。
2. 在开源仓库分别提交实现、验证记录和收尾记录，固定精确实现/验证/收尾 SHA；不访问 upstream、不 push。
3. 检查 Server 目标路径没有重叠脏改动，从本地精确 fetch 收尾 SHA，在 Server `main` 使用普通 `--no-ff` merge；不得复制实现或 cherry-pick。
4. 在 Server 运行定向/完整回归和静态门禁，更新本 change validation 与 `docs/refactor-plan.md`，记录精确 SHA、父提交、祖先关系、未配置 PostgreSQL/未运行 Compose 事实和回滚路径。

## Rollback

恢复 `backend/database.py` 中原校验函数和 `VisualEmbeddingRepository` class，并删除新 repository 模块、包导出、契约/回归测试和本 change artifacts；不回滚 schema、migration 或已有业务数据。
