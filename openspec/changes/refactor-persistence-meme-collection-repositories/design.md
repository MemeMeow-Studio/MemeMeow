## Context

上一切片已经把 ORM 模型、engine、UnitOfWork 和资源装配移到 `backend.persistence`，但 `backend/database.py` 仍在同一文件中实现 Meme/合集、Search、Task、callback、反向图片 Repository 以及文件存储协调。当前 `DataEnvironment` 通过 `backend.database` 的兼容导出组装这些对象，所有业务调用方也继续使用环境上的 scope-bound Repository。

本切片只处理 `MemeRepository` 和 `CollectionRepository`。必须保留现有方法体、SQL 表达式、事务假设和错误码，并通过 facade 显式导出两个类；不能让新模块依赖兼容 facade 的顶层导入。

## Goals / Non-Goals

**Goals:**

- 让 Meme 与合集 Repository 各有一个位于 `backend.persistence.repositories` 的实现来源。
- 保持 `DataEnvironment` 的单 Session、scope 绑定和 lazy assembly 事实。
- 让 `backend.database.MemeRepository` 与 `CollectionRepository` 继续可导入且对象身份稳定。
- 用静态和运行时契约测试锁定导出身份、依赖方向、scope 隔离、分页、成员和导出中间态过滤。

**Non-Goals:**

- 不迁移或修改 SearchRepository、TaskRepository、VisualEmbeddingRepository、callback/反向图片 Repository。
- 不迁移 BlobStore、StorageCoordinator、DatabaseResources、UnitOfWork、engine、ORM 模型、schema 或 migration。
- 不改变任何 SQL、表/列/索引/约束、错误码、文件路径、任务协议、HTTP 或前端行为。

## Decisions

### 1. 按职责拆成两个模块

使用 `repositories/memes.py` 和 `repositories/collections.py`，让每个模块只携带自身查询所需的模型与基础依赖。按方法职责继续拆成更多文件会增加 import 面和装配复杂度；保留一个类一个模块也能直接审查完整 SQL/错误路径。

### 2. Repository 只依赖持久化基础层

新模块直接依赖 `backend.persistence.models`、`backend.persistence.engine.DatabaseError`、路径校验和 SQLAlchemy 基础类型，不导入 `backend.database`。Meme 语境 claim 校验继续使用模型层 `Task`，合集导出继续使用模型层 `StorageOperation`；这两个依赖是既有 SQL 事实，不引入服务层或第二套实现。

### 3. Facade 显式 re-export，资源装配不迁移

`backend.database` 删除两个 class 定义并显式导入新类。`backend.persistence.resources.DataEnvironment` 仍在构造时从 facade 延迟解析 Repository，避免本切片同时改变资源边界和导入拓扑。契约测试直接比较旧路径与新路径的类身份，并检查新模块顶层不存在 facade import。

### 4. 逐字保留行为并补充边界测试

移动类体时保留方法顺序、SQL、分页 clamp、UUID 解析和异常转换；只调整模块级 imports。测试覆盖 facade identity、静态实现唯一性、scope/session 绑定和既有集合边界，避免用新测试替代现有 API/数据库回归。

## Risks / Trade-offs

- [移动时漏掉模型或基础 import] -> 按类体实际引用建立最小 import 集合，运行 import/compileall 和持久化目标测试。
- [facade 导出遗漏导致旧调用失败] -> 旧/新类身份契约测试以及全量 Python 回归。
- [新模块反向导入 facade 形成循环] -> AST 检查顶层 import；仅保留资源装配中已有的延迟 facade import。
- [无意改变 SQL 或中间态过滤] -> 对移动前后类体做逐段 diff，并运行合集/Meme/PostgreSQL marker 测试；未配置 PostgreSQL 时明确 skip。

## Migration Plan

1. 在开源仓库创建新模块、facade 导出、契约测试和 OpenSpec artifacts，运行目标测试、完整回归、strict validate、compileall 与 diff check。
2. 在开源仓库提交实现、验证和收尾记录，固定精确 SHA；不访问或 push 远端。
3. 检查 Server 工作区目标路径没有重叠脏改动，从本地开源仓库精确 fetch 收尾 SHA，并在 Server `main` 以普通 `--no-ff` merge 引入。
4. 在 Server 运行定向/全量回归和静态门禁，更新 `docs/refactor-plan.md` 与本 change validation，核验开源实现/验证/收尾 SHA 是 Server `HEAD` 的真实祖先。
5. 回滚时先恢复 `backend/database.py` 中两个 class 实现，再删除新 repositories 包、契约测试和本 change artifacts；不回滚 schema、migration 或数据。
