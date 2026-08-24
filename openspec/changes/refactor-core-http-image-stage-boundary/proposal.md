## Why

`api.py` 仍把图片处理 Job 查询、显式重试和独立阶段提交与大量图片库业务混在同一入口，HTTP 参数模型、scope 绑定、任务摘要和 Worker 控制面难以单独验证。任务控制域已完成边界提取，现在迁移相邻的图片阶段入口，可以降低入口职责而不改变图片处理状态机。

## What Changes

- 新增公共核心 `backend/image_stage_http.py`，集中承载图片处理 Job 列表/详情/重试，以及独立阶段单图/批量提交的 HTTP 编排和请求模型。
- `api.py` 保留原 route decorator、公开 URL/旧别名、handler 名称和必要的 model/helper 兼容入口，通过 callback 注入当前 scope 的 service、repository、Worker、配置规范化、错误和任务摘要投影。
- 保持 `/images/processing` 读接口、`/image-processing` 隐藏别名、`/images/processing/{job_id}/retry` 重试别名、`/images/stages` 单图和 `/images/stages/batch` 的 method、status、参数校验、scope 隔离、状态投影和稳定错误码。
- Server 的 operation policy/Retry-After 错误投影继续由适配入口 callback 决定；公共模块不复制 Server 商业策略。
- 增加路由快照、旧别名、模块单向依赖、scope repository、stage 校验、批量部分失败、retry 错误和兼容 import 测试。
- 不修改图片处理 repository、Worker 状态机、任务 service、数据库 schema、完整图片库处理入口、Server adapter 或前端。

## Capabilities

### New Capabilities

- `image-stage-http`: 图片处理 Job 读取/重试和独立图片阶段提交的 HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增 `backend/image_stage_http.py`、图片处理/API/安全测试和本 change 的 OpenSpec artifacts。开源实现完成并验证后，Server 只通过精确 commit 的普通 Git merge 同步；不创建 Server 平行实现，不修改数据库、前端或第三方依赖。
