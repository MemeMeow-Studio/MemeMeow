## Why

`api.py` 仍把缓存生成任务的 HTTP 提交入口与大量图片、搜索和任务查询路由混在一起。
`POST /generate-cache` 本身只有当前 scope search service readiness 检查、task service
提交和稳定响应投影，适合成为一个独立的可回滚公共核心边界。

## What Changes

- 新增公共核心 `backend/cache_task_http.py`，集中承载缓存生成任务提交 handler。
- `api.py` 保留原 `generate_cache` route decorator 和同名薄 wrapper，通过 callback 注入
  `_service` 与 `_error`，不改变 scope 解析或 task service 行为。
- 保持 `POST /generate-cache` 的 `202`、`tasks` tag、响应 `task_id/task_type/status`、
  `service_unavailable` 错误以及当前 task service 的幂等提交语义。
- 增加路由、模块依赖、service readiness、响应投影和兼容 import 测试；不修改任务领域、
  数据库 schema、搜索算法或 Server adapter。

## Capabilities

### New Capabilities

无。该 change 只调整公共核心内部 HTTP 模块边界。

### Modified Capabilities

- `cache-task-http`：冻结缓存生成任务 HTTP 提交契约，不增加业务行为。

## Impact

影响公共核心 `api.py`、新增 `backend/cache_task_http.py`、API/task 测试和 OpenSpec artifacts。
开源实现完成并验证后，Server 只通过精确 commit 的普通 Git merge 同步；不创建 Server 平行
实现，不修改数据库、前端或第三方依赖。
