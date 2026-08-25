## Why

`backend/database.py` 仍同时承载 Meme 与合集 Repository、搜索/任务 Repository、文件存储和资源装配，导致最常用的图片与合集数据访问边界难以独立审查。上一切片已经拆出 ORM、engine、事务单元和资源装配；现在提取两个互相独立且已有稳定调用契约的 Repository，可继续降低持久化入口职责密度，同时不改变业务事实。

## What Changes

- 新增 `backend/persistence/repositories/memes.py`，承载 `MemeRepository` 的完整实现。
- 新增 `backend/persistence/repositories/collections.py`，承载 `CollectionRepository` 的完整实现。
- 让 `backend.database` 通过显式兼容导出继续提供两个旧类名，并保持对象身份、构造参数和调用方导入路径。
- 保持 Repository 的 scope 绑定、UnitOfWork 共享 Session、事务/SQL、错误码、分页排序、成员幂等、导出快照和文件操作协作语义不变。
- 增加 Repository 单一实现来源、旧 facade 导出和依赖方向契约测试；不移动搜索/任务 Repository、BlobStore、StorageCoordinator、schema 或 migration。

## Capabilities

### New Capabilities

- `persistence-meme-collection-repositories`: Meme 与合集 Repository 的独立实现来源及旧 `backend.database` 兼容导出契约。

### Modified Capabilities

无。该切片只重构实现边界，不改变公开业务要求。

## Impact

影响公共核心 `backend/database.py`、新增 `backend/persistence/repositories/{__init__,memes,collections}.py`、持久化边界契约测试和本 change artifacts。`DataEnvironment` 仍按原方式装配 Repository；SearchRepository、TaskRepository、BlobStore、StorageCoordinator、数据库 schema/migration、HTTP、任务协议和文件系统事实不变。实现先在开源仓库完成并提交，再按精确 SHA 普通 merge 到 Server。
