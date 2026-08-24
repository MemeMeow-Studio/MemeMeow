## Why

`api.py` 同时承载应用生命周期、通用业务路由和后端 Settings HTTP 入口，导致最小的
设置边界重构也必须穿过一个高耦合模块。阶段 2 需要先完成一个可回滚的入口薄化切片，
将设置 HTTP 契约集中到职责明确的公共核心模块，同时保持既有调用方和公网行为不变。

## What Changes

- 新增公共核心 Settings HTTP/router 模块，集中承载 `ConcurrencyUpdateRequest`、后端
  Settings 状态投影、设置管理 token 的 Header/Bearer 解析与恒定时间校验、dotenv 并发
  更新以及设置路由注册。
- 由该模块注册 canonical 路径 `/backend/settings`、`/backend/settings/concurrency`
  和 legacy 路径 `/settings`、`/backend/settings` 的现有 GET/PATCH/POST 入口；保持
  URL、method、status、响应 key、错误 code、Header 兼容和输入别名不变。
- `api.py` 只保留应用装配、必要的兼容 re-export 和路由模板接入；新模块不得反向
  import `api.py`，并通过单向依赖避免循环导入。
- 增加设置 API 契约测试、路由表快照和旧 import/路径兼容回归，验证环境变量覆盖时
  的 fail-closed 语义与 dotenv 原子更新边界。
- 不规划或修改 search、lifespan、server adapter、schema/migration、frontend 或
  第三方依赖；不改变任何设置 HTTP 的外部可观察行为。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `backend-settings`：冻结 Settings HTTP 域在模块边界重构期间的现有路由、输入、脱敏
  状态投影、授权和持久化契约；本 change 不新增业务行为或改变现有响应。

## Impact

影响公共核心 `api.py`、新增 Settings HTTP/router 模块、`backend/config.py` 的现有
dotenv/Settings API 使用方式，以及设置相关 Python 测试和 OpenSpec delta spec。核心
Settings 模型、环境优先级、并发校验和原子文件更新仍由现有实现提供；不会修改
MemeMeowServer、数据库 schema、前端、网络协议依赖或部署专属授权逻辑。
