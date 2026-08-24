## ADDED Requirements

### Requirement: Settings HTTP 入口必须保持既有路由兼容

系统 MUST 继续注册以下 Settings HTTP 入口，并保持方法、schema 可见性和语义不变：
`GET /backend/settings` 与 `PATCH /backend/settings` 是 canonical 入口；`GET /settings`
与 `PATCH /settings` 是隐藏的 legacy 别名；`POST /backend/settings/concurrency` 是
canonical 并发更新入口；隐藏的 `POST /backend/settings` 继续作为 legacy 并发更新入口。
系统 MUST NOT 新增或删除这些入口的其它 method 别名。

#### Scenario: 路由表保持 canonical 与 legacy 入口
- **WHEN** 应用完成路由装配并读取公开路由表
- **THEN** 路由表包含上述六个 method/path 组合，canonical 入口保持 schema 可见
- **AND** `GET /settings`、`PATCH /settings` 和 `POST /backend/settings` 保持不出现在公开 schema

#### Scenario: 不支持的 Settings method 继续拒绝
- **WHEN** 客户端对 Settings 路径使用未注册的 HTTP method
- **THEN** 系统返回既有 `405` 响应
- **AND** 不读取或写入 Settings、dotenv 或运行时服务

### Requirement: Settings HTTP 输入、状态投影和错误契约必须稳定

并发更新 JSON MUST 只接受正整数，并继续接受 `opencode_concurrency`、
`agent_concurrency`、`concurrency` 和 `value` 四个既有输入别名；未知字段、布尔值、
字符串数字、零值和负值 MUST 被拒绝。查询和保存响应 MUST 保持现有脱敏 Settings
状态投影、`saved`、`restart_required`、`pending` 以及嵌套字段 key，不得返回密钥、完整
token、宿主路径、原始运行时诊断或 dotenv 全文。

#### Scenario: 旧输入别名继续生效
- **WHEN** 授权客户端使用四个既有别名中的任一别名提交正整数
- **THEN** 系统将其解释为同一个 OpenCode 并发字段
- **AND** 成功响应继续返回现有 Settings 状态 key 和待生效值

#### Scenario: 非法输入使用既有校验错误
- **WHEN** 请求提交未知字段、非严格正整数或缺少并发字段
- **THEN** 系统返回 `400` 和 `{"error":"invalid_request","message":...}` 形状
- **AND** 不执行 token 授权后的 dotenv 写入

#### Scenario: 查询状态保持脱敏
- **WHEN** 客户端读取 canonical 或 legacy GET 入口
- **THEN** 系统返回当前有效并发、scope 并发、背压、待生效值、环境覆盖标识、运行时
  与缓存状态以及只读/可编辑/部署分组
- **AND** 响应不包含 API key、设置管理 token、完整 URL、绝对路径或原始探针诊断

### Requirement: Settings 授权和 dotenv 更新必须继续 fail-closed

更新入口 MUST 继续按既有优先级解析 `X-Settings-Admin-Token`、
`X-MemeMeow-Settings-Token` 和大小写不敏感的 `Authorization: Bearer <token>`；缺少
配置、缺少 token、token 不匹配或 token 解析不符合 Bearer 形式时 MUST 统一返回 `403`
和错误 code `settings_forbidden`。比较 token 时 MUST 使用不泄露配置长度或内容的安全比较
语义。当前进程环境中存在 `MEMEMEOW_OPENCODE_CONCURRENCY` 时，系统 MUST 在写入前拒绝
页面更新并返回 `409/settings_environment_override`，不修改 `.env` 或当前有效配置。

#### Scenario: 兼容 Header 和 Bearer 授权
- **WHEN** 授权客户端使用任一兼容 token Header，或使用大小写变体的 Bearer scheme
- **THEN** 合法 token 可以通过授权并继续执行并发更新
- **AND** Header 优先级和空值处理保持既有行为

#### Scenario: 错误凭据 fail-closed
- **WHEN** 设置管理未配置、请求没有 token、token 错误或 Bearer 头格式无效
- **THEN** 更新入口返回 `403`、错误 code `settings_forbidden`
- **AND** 不触碰 dotenv、运行时并发或状态投影中的有效值

#### Scenario: 环境变量覆盖阻止持久化
- **WHEN** `MEMEMEOW_OPENCODE_CONCURRENCY` 存在且授权客户端提交有效新值
- **THEN** 更新入口返回 `409`、错误 code `settings_environment_override`
- **AND** `.env` 内容及当前进程有效并发保持不变

#### Scenario: dotenv 更新失败使用稳定错误
- **WHEN** 授权通过但新值超出当前背压、目标 dotenv 不安全或原子写入失败
- **THEN** 值校验错误返回 `400/settings_update_invalid`，文件安全/写入错误返回
  `409/settings_update_failed`
- **AND** 系统不得返回成功或伪造新的有效配置
