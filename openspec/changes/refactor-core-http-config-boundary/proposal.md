## Why

`api.py` 同时承载应用生命周期、业务路由和 `/config` 运行状态投影。该入口会组合
Settings 脱敏状态、当前 scope 的 search cache、存储预检、视觉服务和 OpenCode runtime
探针，导致一个只读系统接口也必须穿过应用入口的大量上下文。阶段 2 需要先抽取这个
最小公共核心 HTTP 边界，同时冻结其不泄露密钥、路径和诊断原文的安全契约。

## What Changes

- 新增公共核心 `backend/config_http.py`，集中承载 `/config` 的脱敏状态投影和存储预检摘要。
- `api.py` 保留同名兼容 handler wrapper，注入现有 `_request_scope` 与 `_service`，继续在
  原路由模板位置注册 `GET /config`，不改变路由顺序或宿主应用装配。
- 保持 `200` 响应 key、scope 展示开关、search cache、reverse-image/visual 状态、runtime
  固定字段和 storage preflight 摘要语义；不暴露 API key、绝对路径、完整 URL 或原始诊断。
- 增加模块依赖、脱敏字段、scope/cache fallback 和路由快照测试，证明新模块不反向导入
  `api.py`。

## Capabilities

### New Capabilities

无。该 change 只调整公共核心内部 HTTP 模块边界。

### Modified Capabilities

- `config-http`：冻结 `/config` 路由、状态投影和信息脱敏契约；不新增业务行为。

## Impact

影响公共核心 `api.py`、新增 `backend/config_http.py`、配置/运行时测试和 OpenSpec artifacts。
不修改数据库 schema、迁移、前端、Server 适配层或第三方依赖。开源实现完成并验证后，
Server 只通过该精确开源 commit 的普通 Git merge 同步，不使用平行实现。
