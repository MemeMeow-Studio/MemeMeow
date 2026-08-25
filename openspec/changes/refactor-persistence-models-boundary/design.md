## Context

当前 `backend/database.py` 的 ORM 声明位于数据库错误类型之后、engine/UoW 和 Repository 之前，约占模块前 770 行。模型之间通过表级外键、约束和索引共同构成单一 metadata；`alembic/env.py`、启动兼容 DDL 和业务代码都通过 `backend.database` 读取这些对象。该切片只移动声明位置，不能产生第二套 Base 或第二份表定义。

## Goals / Non-Goals

**Goals:**

- 让 `backend/persistence/models.py` 成为 ORM 模型、`Base`、`ScopeContext`、模型常量和可选控制表集合的唯一声明来源。
- 让 `backend.database` 继续提供所有现有领域模型、常量、`utcnow` 和 metadata 相关导出。
- 保持导入顺序安全：模型模块不能反向导入 `backend.database`，从而为后续持久化层拆分保留依赖方向。
- 通过静态和运行时契约测试锁定模型身份、metadata 表集合及关键索引/约束事实。

**Non-Goals:**

- 不拆分 engine、UnitOfWork、Repository、BlobStore、StorageCoordinator 或 `DatabaseResources`。
- 不改变 Alembic migration、启动兼容 DDL、表/列/字段类型、默认值、约束、索引或 PostgreSQL 行为。
- 不迁移调用方导入路径，不要求业务模块立刻改用新路径。

## Decisions

### 1. 单文件模型模块

本切片把全部 ORM 声明放在一个 `models.py`，保留类的原有顺序，以便复合外键和跨表索引继续在同一 metadata 注册。按领域拆成多个 model 文件会引入新的导入排序和 metadata 初始化风险，留到后续有明确边界时再做。

### 2. 兼容 facade 使用显式 re-export

`database.py` 从模型模块显式导入领域公开对象，而不是让调用方改用新路径或通过动态 `__getattr__` 兜底。这样旧 import 在静态检查、类型分析和 Alembic 运行时都保持可见，模型对象身份也能由契约测试直接比较。

### 3. 时间函数随模型移动

模型默认值使用的 `utcnow` 与 `EMBEDDING_DIMENSIONS`、`VISUAL_EMBEDDING_DIMENSIONS` 同属声明契约，移动到 models 后由 database facade re-export。`SCOPE_LOCAL`、schema revision 和任务 lane 协议仍留在 database，因为它们服务于资源/Repository 行为而非 ORM 声明。

### 4. 以当前 metadata 做回归基线

测试记录模型模块的表名集合、固定向量维度、`OPTIONAL_CONTROL_TABLES` 成员和关键 callback/fairness 约束；不生成新的迁移，也不以 `create_all` 结果替代 Alembic 事实。

## Risks / Trade-offs

- [导入 facade 遗漏模型或常量] -> 建立完整导出身份表测试，并运行全量 Python 回归。
- [重复声明导致 metadata 分叉] -> 测试 `backend.database` 与 `backend.persistence.models` 的对象身份及唯一 metadata。
- [跨表声明顺序变化破坏外键/索引] -> 保留原声明顺序，运行模型契约、compileall 与完整测试。
- [回滚时删除新模块过早] -> 回滚顺序为先恢复 `database.py` 原声明，再删除 models 包和契约测试；schema/migration 无需回滚。

## Migration Plan

1. 在开源仓库创建 models 包、兼容 facade 和契约测试，运行目标测试、完整回归、OpenSpec strict、compileall 与 diff check。
2. 提交实现、验证和收尾记录的精确 SHA；在获授权后从本地开源仓库 fetch 精确收尾 SHA。
3. 在 Server `main` 检查重叠脏改动后以普通 `--no-ff` merge 引入精确 SHA，运行 Server 定向及完整回归，并记录祖先关系。
4. 回滚时恢复 `database.py` 的原模型声明并删除 `backend/persistence` 和本 change 新增测试/artifacts，不修改 schema 或 migration。
