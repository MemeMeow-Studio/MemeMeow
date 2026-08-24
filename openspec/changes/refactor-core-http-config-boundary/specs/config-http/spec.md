## ADDED Requirements

### Requirement: Config HTTP route and response must remain compatible

系统 MUST 继续注册公开 `GET /config` 路由，method、path、`system` tag、handler 名称和
路由相对顺序保持不变。成功响应 MUST 保持既有脱敏 Settings 状态 key，并继续根据应用
`expose_scope` 开关决定是否返回当前 scope 标识。

#### Scenario: Route snapshot remains stable

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** 路由表只包含一个公开 `GET /config` APIRoute
- **AND** 该路由仍位于 `/health` 之后、内部业务路由之前，且不新增其它 method alias

#### Scenario: Scope visibility follows server policy

- **WHEN** 请求带有已绑定 scope 且 `expose_scope` 为真
- **THEN** 返回当前请求 scope 的 `scope_id`
- **WHEN** `expose_scope` 为假
- **THEN** 响应不包含 `scope_id`

### Requirement: Config state must stay sanitized and scope-bound

`/config` MUST 继续返回 search cache、database、storage preflight、reverse-image、visual
和 runtime 的现有布尔/固定字段；runtime 探针只能投影固定白名单 key。响应 MUST NOT 包含
API key、设置凭据、宿主绝对路径、完整 URL、文件名、原始 runtime 诊断或 storage 报告内容。

#### Scenario: Runtime diagnostics are reduced to fixed fields

- **WHEN** runtime probe 含有任意额外标识、路径或诊断文本
- **THEN** 响应只保留既有固定 runtime key 和布尔 `runtime_ready`
- **AND** 额外字段不会进入 JSON

#### Scenario: Storage summary exposes counts only

- **WHEN** storage preflight report 含有文件名列表、阻断错误列表和 orphan files
- **THEN** 响应只返回 `status`、各类数量和 `blocking_errors` 数量
- **AND** 不返回任何原始列表项

### Requirement: Config implementation must preserve dependency direction

公共核心 config HTTP 模块 MUST NOT import `api.py` 或 Server 入口；scope/service 解析 MUST
通过入口已有 callback 复用，不能回退到跨请求 singleton 或绕过 scope 校验。

#### Scenario: Module is independently importable

- **WHEN** 静态检查 config HTTP 模块的 import graph
- **THEN** 不包含 `api` 或 `server_api`
- **AND** `api.config_status` 仍可被旧 Python 调用方导入
