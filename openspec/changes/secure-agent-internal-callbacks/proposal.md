## Why

Agent 内部视觉匹配和反向图片检索接口目前只凭 `task_id` 反查 scope，并把 Compose 内网可达性当作信任边界；知道活动任务 ID 的调用方可以借用任务能力，旧 claim 也可能在任务被重新认领后继续产生副作用。公网宿主部署需要由开源核心提供与用户鉴权分离的服务间认证和当前执行权校验，同时保留适配宿主替换验证方式的能力。

## What Changes

- 新增可注入的 Agent 内部 callback verifier，先验证服务间凭据，再读取请求体、查询 Task 或解析 scope；启用 callback 但缺少有效 verifier 时必须 fail-closed。
- 为每次 Agent 执行注入受服务端管理、绑定 `task_id`、scope、claim generation/owner、允许操作、目标 SHA 和有效期的 callback 执行凭据；仅知道 `task_id` 或仅持有未绑定任务的服务凭据不能调用业务能力。
- callback 只从持久 `Task.scope_id` 和受验证的当前 claim 构造 scope 服务，并继续校验任务类型、`running` 状态、租约、目标 Meme/SHA、attempt 和请求幂等事实；客户端字段不能覆盖这些事实。
- 将反向图片和视觉匹配纳入同一 callback 安全边界；反向图片不得借用一个任务检索任意图片，视觉匹配不得只校验当前 Meme 与 embedding 而忽略任务目标 SHA。
- 认证失败、旧 claim、跨 scope、错误任务类型、目标变化和重放必须在 provider、usage、缓存或业务写入之前拒绝，并使用不泄露 Task 是否存在的稳定错误。
- 开源部署提供显式配置的默认服务凭据与任务执行凭据实现；适配宿主可以注入 token、HMAC 或 mTLS verifier，但用户认证、订阅和 operation policy 保持独立。
- **BREAKING** 现有无认证 Agent callback 不再可用；内部 CLI 和 Runner 必须携带服务端注入的 callback 凭据与当前执行绑定。

## Capabilities

### New Capabilities

- `agent-internal-callbacks`: 定义 Agent 到核心 API 的服务间认证、当前 claim 绑定、scope/目标恢复、重放防护、fail-closed 装配和稳定拒绝语义。

### Modified Capabilities

无。尚未归档的 `add-task-scoped-reverse-image-search` change 直接修订其新增 capability，本 change 只定义可被所有 Agent callback 复用的通用安全边界。

## Impact

- 影响 FastAPI `/internal/reverse-image/search`、`/internal/visual-search/match` 及后续 Agent callback 的路由前置校验、请求模型和错误响应；反向图片的具体 Requirement 在其尚未归档的原 change 中同步收紧。
- 影响 Agent Runner/executor、Skill 薄客户端、callback 配置、secret 传递和日志脱敏；API 到 executor 的 Bearer token 与 Agent 到 API 的 callback 凭据必须分离。
- 复用持久 Task、claim/lease fencing、`ScopeServiceFactory` 和目标 SHA 事实，不新增 User、Account、Subscription、Payment 或商业授权实体。
- 与 `make-application-scope-aware` 的 factory scope 一致性约束、`introduce-image-processing-worker` 的持久 Agent Task/attempt，以及 `add-operation-policy-hooks` 的 grant 生命周期协同实施。
