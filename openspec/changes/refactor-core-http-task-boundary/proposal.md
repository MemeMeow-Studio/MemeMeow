## Why

`api.py` 仍同时承载任务查询、取消、重试路由和公开任务摘要投影，导致 HTTP 契约、安全脱敏、Agent 活跃度读取与任务服务装配耦合。`/generate-cache` 已经完成边界提取，现在继续迁移相邻的任务控制域，可以减少入口职责而不改变任务领域事实。

## What Changes

- 新增公共核心 `backend/task_http.py`，集中承载任务摘要投影、Agent 活跃度批量读取以及任务列表、详情、取消和重试 handler。
- `api.py` 保留原 route decorator、handler 名称和 `_task_summary` 等旧 import 兼容入口，通过 callback 注入 scope-bound service、图片处理 repository、错误投影和 OpenCode 取消能力。
- 保持 `/tasks`、`/tasks/{task_id}`、`/tasks/{task_id}/cancel` 和 `/tasks/{task_id}/retry` 的 URL、method、status、查询参数、错误码、响应字段、scope 绑定和脱敏规则。
- 增加 route snapshot、模块单向依赖、摘要脱敏、活跃度容错、续跑策略、取消/重试错误和旧 import 兼容测试。
- 不修改任务服务、图片处理 repository、数据库 schema、调度器、Server adapter 或前端。

## Capabilities

### New Capabilities

- `tasks-http`: 任务控制 HTTP 路由、公开摘要和安全诊断字段的兼容契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增 `backend/task_http.py`、任务/API 测试和本 change 的 OpenSpec artifacts。开源实现完成并验证后，Server 只通过精确 commit 的普通 Git merge 同步；不创建 Server 平行实现，不修改数据库、前端或第三方依赖。
