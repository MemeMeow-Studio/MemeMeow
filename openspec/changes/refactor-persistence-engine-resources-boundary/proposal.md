# 持久化 engine、事务与资源边界

## Why

`backend/database.py` 已经把 ORM 声明交给 `backend.persistence.models`，但仍同时负责
连接/启动门禁、事务单元、scope 环境和进程级资源装配。这样任何持久化基础设施审查都
必须触及 Repository、BlobStore 和 StorageCoordinator 的大文件实现，增加了导入循环与
回归风险。本切片继续沿现有依赖方向拆出三块稳定职责。

## What Changes

- 新增 `backend/persistence/engine.py`，承载数据库 URL、Engine 创建、可选控制面 schema
  兼容补齐、数据库检查和 local 初始化，并提供 `DatabaseError` 的单一来源。
- 新增 `backend/persistence/unit_of_work.py`，承载同步 `UnitOfWork` 事务边界。
- 新增 `backend/persistence/resources.py`，承载 `DataEnvironment` 和
  `DatabaseResources` 的 scope-bound 运行时装配。
- `backend.database` 通过显式 re-export 保留上述旧符号及现有模型、Repository、
  BlobStore、StorageCoordinator 的 import 路径；业务调用方无需迁移。
- 增加 engine/UoW/resources 边界与兼容 facade 契约测试，锁定对象身份、依赖方向、
  session/资源生命周期和现有 schema 行为。

## Capabilities

### New Capabilities

- `persistence-engine-resources-boundary`: engine、UnitOfWork、DataEnvironment 和
  DatabaseResources 的单一实现来源及旧 facade 兼容契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `backend/database.py`、新增 `backend/persistence/engine.py`、
`unit_of_work.py`、`resources.py`、持久化契约测试和本 change artifacts。Repository、
BlobStore、StorageCoordinator、Alembic 读取的 metadata、migration、schema/约束/索引、
HTTP 与任务协议均保持原实现。实现先在开源仓库完成并提交，再按精确 SHA 普通 merge 到
Server。
