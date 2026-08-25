## Why

`backend/database.py` 仍同时实现 SearchRepository、其它 Repository、文件存储和资源装配，搜索控制面与查询逻辑难以独立审查。上一阶段已经建立 `backend.persistence` 的模型、engine、事务和 Repository 边界，现在提取 SearchRepository 可以继续降低入口职责密度，同时保留已有搜索协议和迁移安全语义。

## What Changes

- 新增 `backend/persistence/repositories/search.py`，承载 SearchRepository 的完整实现及其 generation/head、migration state、legacy/incremental text embedding 查询和向量排序逻辑。
- 让 `backend.database` 删除重复实现并显式 re-export 同一个 SearchRepository 类，保持历史 import 路径与类身份。
- 保持 `DataEnvironment` 的 scope-bound Session 装配、Repository 构造参数、SQL、事务边界、错误码、查询排序、generation 状态切换和 fail-closed 校验不变。
- 增加 SearchRepository 的实现唯一来源、facade 兼容、单向依赖、scope/迁移来源和查询排序契约测试；补充本 change validation 记录。
- 不移动 VisualEmbeddingRepository、TaskRepository、ReverseImageUsageRepository、BlobStore、StorageCoordinator、schema/migration、HTTP、frontend 或其它 active change。

## Capabilities

### New Capabilities

- `persistence-search-repository`: SearchRepository 的独立持久化实现来源、旧 facade 兼容和 generation/migration/text embedding 查询契约。

### Modified Capabilities

无。本 change 只重构实现边界，不改变公开业务要求。

## Impact

影响公共核心 `backend/database.py`、新增 `backend/persistence/repositories/search.py`、Repository 包入口、SearchRepository 契约测试和本 change artifacts。`backend.persistence.resources.DataEnvironment` 继续通过已有兼容 facade 延迟装配，不改变共享 Session 或 scope 事实；数据库 schema、Alembic migration、任务协议、HTTP、前端和视觉向量 Repository 均不变。实现先在 `/home/infstellar/vscode/MemeMeow` 完成并提交，再按精确 SHA 从本地 fetch 到 Server，以普通 `--no-ff` merge 引入；不访问 upstream 或 push。
