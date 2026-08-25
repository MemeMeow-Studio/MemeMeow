## Why

`backend/database.py` 仍同时承载 BlobStore、StorageCoordinator、文件安全边界和跨存储恢复逻辑；这使文件一致性、durable fact 与 lease fencing 的审查范围和回滚边界不清晰，也让后续持久化模块无法形成完整职责域。现在将已有语义聚合到一个 canonical storage 模块，同时保留 `backend.database` 的历史导入，能够在不改变数据库事实或公开 API 的前提下收紧边界。

## What Changes

- 新增 `backend/persistence/storage.py`，集中实现 BlobStore 与 StorageCoordinator，成为文件解析、暂存、写入、删除、rename、隔离、恢复和完整性扫描的唯一来源。
- 让 `backend/persistence.resources` 直接装配 canonical storage 实现；`backend.database` 只通过显式 re-export 保留旧导入路径，不再拥有文件存储实现。
- 保留并以契约测试锁定 storage operation 的 prepared/file_applied/completed/compensated/blocked 状态、指纹校验、CAS/lease fencing、未知执行 fail-closed、durable fact 与 grant/lease 释放边界。
- 增加模块边界、旧 import、失败/恢复、权限/路径和资源一致性的定向测试，并在同一聚合 change 中记录实现、验证、同步和收尾 SHA。
- 不新增 schema revision，不改变现有表/列/约束、HTTP 路由、scope 协议或 Server 专属控制面。

## Capabilities

### New Capabilities

- `persistence-storage-consistency`: 文件存储与结构化持久化之间的 scope、安全、一致性、恢复和兼容导入契约。

### Modified Capabilities

无。现有 `scoped-persistence` 的行为保持不变，本 change 将其文件一致性实现拆成可审查的 canonical 职责域。

## Impact

影响公共核心 `backend/database.py`、`backend/persistence/resources.py`、新增
`backend/persistence/storage.py`、storage operation 模型的调用边界和持久化契约/失败恢复测试；不引入依赖或 migration。Server 只有在开源聚合实现、验证和收尾 SHA 经过审核/授权后，才从本地精确历史普通 merge；不访问 upstream、不 push。
