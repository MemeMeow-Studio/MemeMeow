## Why

MemeMeow 需要在扁平图片库之上提供用户可见的组织能力，让同一张 Meme 可以同时属于多个主题合集。PostgreSQL 重构已经提供稳定 `meme_id` 和 scope-bound 数据边界，`remove-image-directories` change 将先删除目录模型，因此可以在不复制图片、不耦合用户系统的前提下增加逻辑合集。

## What Changes

- 新增 scope 内的 Meme 合集，支持创建、查看、重命名和删除合集。
- 支持将当前 scope 的多张 Meme 批量加入合集，以及从合集移除成员；同一 Meme 可以属于多个合集。
- 合集成员始终引用稳定 `meme_id`，图片重命名不会改变合集关系。
- 删除合集只删除合集及成员关系，不删除 Meme；删除 Meme 时自动清理其合集关系。
- 开源适配层继续固定使用 `local` scope，不新增用户、登录或客户端可选的 scope；未来宿主身份层可以把可信用户映射到独立 scope。
- 新增合集 REST API，并在前端提供合集浏览、管理以及从图片库批量加入合集的工作流。
- 依赖 `remove-image-directories` 完成 migration 基线固化和扁平图片库收敛，再以前向 migration 增加合集表。
- 首版不实现合集共享、权限授予、嵌套合集、手工排序、独立封面、导出格式或每合集独立向量索引。

## Capabilities

### New Capabilities

- `meme-collections`: 定义 scope 隔离的逻辑合集、成员关系、管理 API 和用户工作流。

### Modified Capabilities

无。

## Impact

- PostgreSQL ORM、scope-bound repository、`DataEnvironment` 和 Alembic migration。
- FastAPI 请求模型、合集路由和统一错误契约。
- Vue API 客户端、图片库多选操作、合集列表及详情界面。
- 数据库静态契约、PostgreSQL 集成测试、API 测试、前端组件测试和端到端测试。
- 不改变图片文件布局、`BlobStore`、现有媒体 URL、语境任务或 pgvector generation 模型。
- 实现与归档顺序位于 `remove-image-directories` 之后。
