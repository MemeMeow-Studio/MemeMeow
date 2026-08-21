## Why

Agent callback 当前把客户端提交的 `request_id` 当作反向图片 usage、provider 调用和 operation grant 的幂等键。`validate_request_binding()` 只验证格式，因而同一有效 Task claim 可以用另一个 request ID 携带相同目标和规范化检索输入，重新进入 provider/usage/grant 路径，尤其会绕过 `refresh=true` 下的缓存复用边界。

## What Changes

- 为反向图片 callback 定义服务端权威的逻辑请求身份：由当前 scope、Task claim generation/attempt、operation、目标图片事实和规范化图片/检索输入稳定生成，并与一个权威 `request_id` 一对一绑定。
- 复用现有 `agent_callback_requests`、`reverse_image_usage_events` 和 operation grant 事实；同一逻辑请求无论客户端省略、重试或更换 request ID，都只能恢复同一个权威事实，不得新建 provider、usage 或 grant 副作用。
- 将客户端 `request_id`、`input_digest`、query、refresh、裁剪和其他参数变成非权威提示；服务端在规范化后重新计算输入摘要，已存在的 request ID 改输入、逻辑输入改绑、跨 Task/scope/attempt 或历史事实无法确认时，沿用稳定冲突或 `unknown_execution` 语义 fail-closed。
- 以数据库唯一约束和并发安全的查找/插入顺序保护首次请求、不同 ID 竞态、进程崩溃和 provider 已开始但结果未知的恢复；评估并执行必要的前向迁移，不猜测覆盖历史副作用事实。
- 允许 callback 客户端省略 `request_id`，保持 Host、Docker、executor 和薄 CLI 的地址/凭据传递兼容；local 直连模式继续保持既有显式 request ID 和缓存语义。
- 补充 API、服务、PostgreSQL/内存事实层和 Host/Docker/executor/CLI 兼容测试，覆盖规范化输入、`refresh`/`query`、并发、崩溃恢复、旧 claim、重复 ID、跨 Task/scope/attempt 和未知外部结果。
- 不新增公网 IP 限流、在途限制、账户 quota、订阅计费或新的 operation quota owner；不重新设计既有 callback 服务认证、反向图片 provider 或 operation grant 生命周期。

## Capabilities

### New Capabilities

- `agent-callback-request-binding`: 定义 Agent callback 逻辑请求身份、权威 request ID、重放/并发/崩溃恢复和反向图片 usage/provider/grant 的幂等边界。

### Modified Capabilities

无。现有 `secure-agent-internal-callbacks`、`add-task-scoped-reverse-image-search`、`harden-operation-grant-association` 和 `add-operation-policy-hooks` 是本 change 的前置/协作 active changes；本 change 为其中已声明但未可靠落地的 callback request binding 补充独立公共契约，不重复定义其认证、策略或供应商协议。

## Impact

- 影响 `backend.callbacks` 的输入规范化/绑定校验、`backend.reverse_image` 的 request 解析和 usage/grant 编排、`backend.database` 的 callback 事实 repository 及必要的 Alembic schema/index。
- 影响 `/internal/reverse-image/search`、反向图片薄 CLI、Runner 注入的可选 request ID 传递和 callback 响应中的权威 request ID；不改变 callback token、Task claim 或 provider secret 的信任边界。
- 影响内存与 PostgreSQL 单元/集成测试、并发与重启恢复夹具，以及 local、Host、Docker、executor 运行模式回归。
