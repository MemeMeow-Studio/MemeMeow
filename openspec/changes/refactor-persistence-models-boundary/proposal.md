## Why

`backend/database.py` 同时承载 ORM 声明、事务边界、多个 Repository、BlobStore、StorageCoordinator 和资源装配，模型变更的审查范围因此被不必要地放大。当前阶段先抽出模型声明，可降低持久化模块职责密度并为后续 engine、Repository 拆分建立稳定入口，同时不改变任何数据库事实。

## What Changes

- 新增 `backend/persistence/models.py`，集中声明 Base、ScopeContext、全部 ORM 模型、可选控制表集合及模型相关常量/时间函数。
- 新增 `backend/persistence` 包入口，明确模型模块属于持久化层。
- `backend/database.py` 改为兼容 facade，通过显式 re-export 保留现有模型、常量、`utcnow` 和 `OPTIONAL_CONTROL_TABLES` 导入路径。
- 增加模型边界与兼容导出的契约测试，验证 metadata、表名和模型身份只有一份来源。
- 不修改 schema、migration、字段、约束、索引、Repository、UnitOfWork、BlobStore、StorageCoordinator 或资源装配行为。

## Capabilities

### New Capabilities

- `persistence-model-boundary`: 持久化 ORM 声明的单一来源和旧 `backend.database` 导入兼容契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `backend/database.py`、新增 `backend/persistence/models.py` 与包入口、模型契约测试及本 change artifacts。迁移入口继续从 `backend.database.Base` 读取同一个 SQLAlchemy metadata；不新增依赖、不触碰 HTTP、文件存储或任务协议。实现先在开源仓库验证并提交，再按精确 SHA 普通 merge 到 Server。
