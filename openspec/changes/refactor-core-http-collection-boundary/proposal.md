## Why

`api.py` 仍把合集列表、创建、详情成员投影、重命名、删除和成员维护与 ZIP 导入导出及其它图片路由混在同一入口中。合集 CRUD/详情/成员操作可以独立提取为公共 HTTP 边界，同时保留 Server 已接管的导出安全边界和现有导入实现。

## What Changes

- 新增公共核心 `backend/collection_http.py`，集中承载合集列表、创建、详情、重命名、删除和成员增删的 HTTP 编排。
- `api.py` 保留合集请求模型、canonical route/query 声明和旧 handler 名称，通过 callback 注入当前 scope environment、metadata service 和错误工厂。
- 保持合集 scope 绑定、分页顺序、名称唯一性、成员稳定 `meme_id`、状态投影、幂等成员操作、错误码和响应字段。
- 明确不迁移 `/collections/import` 与 `/collections/{collection_id}/export`；导入继续留在公共入口，导出继续由 Server collection export boundary 覆盖。
- 增加路由、依赖方向、scope/query 拒绝、CRUD/详情/成员响应和错误映射契约测试。

## Capabilities

### New Capabilities

- `collection-http`: 合集 CRUD、详情和成员维护 HTTP 边界契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增合集 HTTP 模块、合集 HTTP 契约测试和本 change artifacts。实现先在开源仓库验证并提交，再按用户审核通过的精确 SHA 普通 merge 到 Server；不创建 Server 平行实现，也不修改合集数据库 schema、导入包协议、Server 导出限制或前端。
