## Why

`api.py` 仍把内部视觉匹配 callback 的 token 绑定、scope 资源、幂等 callback fact、视觉 service 和错误投影混在公共入口中。这个路由不需要依赖其它 HTTP handler，单独提取可以降低 callback 安全边界的审查范围。

## What Changes

- 新增公共核心 `backend/visual_callback_http.py`，集中承载 `VisualMatchRequest` 和 `/internal/visual-search/match` handler 编排。
- `api.py` 保留 canonical route、旧 handler 名称和模型 import，通过 callback 注入 binding、registration、database、scope service factory 和错误工厂。
- 保持 callback token/task claim/attempt 校验、request_id/input_digest 绑定、started/completed/failed fact、scope 派生、视觉 service 错误映射和幂等响应。
- 不修改 callback token 生成、数据库 repository、VisualSearchService、Server middleware、数据库 schema 或前端。
- 增加 route、依赖方向、绑定拒绝、request_id 幂等、fact 状态收束和 legacy import 测试。

## Capabilities

### New Capabilities

- `visual-search-callback-http`: 内部视觉匹配 callback HTTP 契约。

### Modified Capabilities

无。

## Impact

影响公共核心 `api.py`、新增 callback HTTP 模块、相关契约测试和本 change artifacts。公共实现先在开源仓库完成并验证，再按精确 SHA 普通 merge 到 Server；不创建 Server 平行实现。
