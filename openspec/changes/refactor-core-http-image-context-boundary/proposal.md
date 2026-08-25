## Why

`api.py` 仍把图片语境、视觉向量和 metadata repair 的 HTTP 输入、scope 目标解析、任务投影与批量部分失败混在图片库入口中。它们已经共享图片处理 Job 控制面，但不需要直接依赖入口模块；独立边界可以降低 route handler 的职责密度并复用现有安全测试。

## What Changes

- 新增公共核心 `backend/image_context_http.py`，集中承载图片语境、视觉向量和 metadata repair 请求模型及 handler 编排。
- `api.py` 保留 canonical route、handler 名称和旧模型 import，通过 callback 注入当前 scope service、environment、处理 Job 提交、任务 service、错误映射和稳定 enqueue error。
- 保持 `/images/context`、`/images/context/batch`、`/images/visual-embedding`、`/images/visual-embedding/batch` 和 `/images/metadata/repair` 的 method、status、scope 目标派生、任务响应、批量跳过/失败和稳定错误码。
- 不修改图片处理 Worker、任务 service、metadata repository、数据库 schema、Server adapter 或前端。
- 增加 route/alias、模块单向依赖、scope 目标、批量隔离、旧 import 和 metadata repair callback 测试。

## Capabilities

### New Capabilities

- `image-context-http`: 图片语境、视觉向量和 metadata repair HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增 `backend/image_context_http.py`、相关 HTTP 契约测试和本 change artifacts。实现先在开源仓库完成并验证，再按精确 commit 普通 merge 到 Server；不创建 Server 平行公共实现。
