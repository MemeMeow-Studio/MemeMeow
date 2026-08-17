## Why

公共 operation grant 关联目前主要按 `scope/operation/idempotency_key` 命中。相同幂等键携带不同资源、任务、来源、units 或输入摘要时，内存和 PostgreSQL store 仍可能返回原 grant；已 `released`、`committed` 或 `unknown` 的关联也可能被入口当成新的执行授权。公网宿主需要一个不理解账户、套餐或周期的通用事实校验边界，避免冲突请求复用授权或在崩溃后重放副作用。

## What Changes

- 为 `OperationRequest` 增加服务端输入摘要和规范化请求指纹，覆盖 `resource_id`、`task_id`、`source`、`units` 与 `input_digest`。
- 让内存和 PostgreSQL grant association store 在命中时比较完整可信事实；事实冲突、旧行缺少可验证事实或数据状态无法判断时沿用 `operation_policy_unavailable` fail-closed 语义。
- 仅 `acquired` association 可以作为 acquire 的执行授权；`committed`、`released` 和 `unknown` 仅可被恢复/审计路径观察，不能重新 acquire 或触发 provider、上传、删除和 Agent 外部执行。
- 为 pipeline grant 的后置 Task 绑定更新可信指纹，并把指纹事实持久化；补齐普通上传、合集导入、删除、pipeline/standalone Agent、反向图片和 Worker 恢复路径的服务端输入摘要。
- 新增前向 schema migration；历史 operation grant 行若没有完整新事实不回填猜测值，而由 store 保持 fail-closed。

## Capabilities

### New Capabilities

- `operation-grant-association`: 定义 grant 关联请求事实校验、可执行状态门禁、持久化迁移和受影响调用路径的通用契约。

### Modified Capabilities

无。既有 operation vocabulary、稳定错误码和默认 allow-all 行为保持不变。

## Impact

- 影响公共 operation policy 模块、operation grant ORM、Alembic migration 和 grant 命中调用方。
- 影响内存单元测试、PostgreSQL 集成测试、上传/删除 API、图片处理 Worker、反向图片 provider 及其恢复路径。
- 不新增账户、订阅、套餐、支付、额度数值或周期算法；宿主计量实现只依赖本 change 提供的通用关联契约。
