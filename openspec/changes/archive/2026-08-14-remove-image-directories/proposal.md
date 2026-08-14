## Why

MemeMeow 即将使用合集承担用户可见的组织职责，继续保留“根目录、创建目录、目标目录”会形成两套相互竞争的分类模型。当前实例没有子目录数据，因此可以直接删除目录契约，让图片库成为单一扁平资源集合，而不承担兼容和迁移成本。

## What Changes

- **BREAKING** 删除图片目录的创建、列出和浏览能力，不保留旧端点、别名或兼容响应。
- **BREAKING** `GET /images` 删除 `directory` 参数以及响应中的 `directory`、`directories` 字段，只分页列出当前 scope 的全部 Meme，并保留文件名筛选。
- **BREAKING** `POST /images/upload` 删除 `directory` 表单字段，所有图片直接保存到当前 scope 的图片根。
- 图片重命名继续只接受稳定 `meme_id` 和新文件名，目标始终位于当前 scope 图片根，不再返回目录字段。
- 业务 `storage_key` 收敛为不含路径分隔符的单个安全文件名；migration 和应用启动通过只读预检拒绝非扁平业务记录及嵌套业务图片，不自动搬移、导入、修复或兼容子目录。
- 前端删除“根目录”“创建目录”和“目标目录”控件，图片库始终展示当前 scope 的全部图片。
- 内部 `.staging` 和 `.quarantine` 存储区域继续由 BlobStore 使用，但不成为业务目录或公共 API。
- 固化首版 Alembic schema，避免历史 migration 动态读取未来 ORM 模型；新增前向 migration 加入扁平 `storage_key` 数据库约束。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `image-library`: 将目录化图片浏览和目录创建改为 scope 内扁平图片列表，并收敛重命名及媒体访问契约。
- `image-ingestion`: 将上传目标从用户选择目录改为当前 scope 的唯一图片根。

## Impact

- FastAPI 图片列表、目录、上传和重命名接口存在有意的兼容性破坏。
- Vue 图片库、上传工作流及其 API 客户端删除全部目录状态和控件。
- PostgreSQL `memes.storage_key` 增加扁平文件名约束；repository、BlobStore 业务入口和存储协调器同步收紧校验。
- Alembic 基线和 migration 测试需要覆盖空库重放、已有扁平数据升级以及非扁平数据拒绝。
- 后续 `add-meme-collections` change 依赖本 change，并使用合集作为唯一用户可见组织单元。
