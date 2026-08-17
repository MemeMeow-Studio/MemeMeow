## Context

`backend.operation_policy` 位于上传、图片处理 Worker、反向图片 provider 和删除操作的真实副作用边界。现有关联主键只能证明 scope、operation 和逻辑幂等键相同，不能证明本次请求的资源、Task、来源、单位或输入摘要相同；PostgreSQL 旧表还没有保存 source、units 和完整指纹。调用方先 `get` 再把结果当作 grant 返回时，terminal 或 unknown association 会绕过新的 acquire 边界。

公共核心必须继续不知道用户、账户、套餐、支付和周期，只负责验证宿主提供的服务端事实。所有新事实由可信服务代码构造，客户端字段仍由 gateway 丢弃。

## Goals / Non-Goals

**Goals:**

- 统一生成可比较的请求指纹，并在内存和 PostgreSQL store 中执行事实一致性校验。
- 将 `acquired` 定义为唯一可执行 association 状态，保留 terminal/unknown 供恢复路径观察。
- 让重复 acquire 在事实完全一致时复用原 grant，在冲突或旧数据不可验证时稳定 fail-closed。
- 覆盖 API 上传/删除、合集导入、pipeline/standalone Agent、反向图片和 Worker 恢复的请求事实传递。
- 用前向 migration 保存新字段，不对历史缺失事实做不安全猜测。

**Non-Goals:**

- 不新增 operation vocabulary 或公共错误码。
- 不实现账户、订阅、套餐、额度、容量和周期算法。
- 不改变宿主 policy 的 quota、reservation 或外部计量实现。

## Decisions

### 1. 指纹覆盖显式服务端请求事实

`OperationRequest` 的 `resource_id`、`task_id`、`source`、`units` 和可选 `input_digest` 先经过现有字段校验，再按稳定 JSON 顺序计算 SHA-256。scope、operation 和幂等键仍由关联主键及 `GrantRef` 绑定；客户端提交的 scope、user、grant 等字段继续被 gateway 丢弃。比较时同时检查原始字段和摘要，避免只依赖字符串哈希的误用。

`input_digest` 使用现有 64 位十六进制摘要约束。上传和合集导入使用图片 SHA，Agent 使用目标图片 SHA，反向图片使用已验证的 callback/请求输入摘要；删除等没有额外输入摘要的操作显式使用 `None`，不以空字符串伪造事实。

### 2. 关联 store 的命中和执行分离

`get` 可以返回已验证的 terminal association，供恢复器判断“不得重放”；`acquire` 必须在相同事实下且仅在 `acquired` 状态返回 grant。事实冲突、指纹缺失、非法状态和无法确认的持久行统一抛出既有 `operation_policy_unavailable`，不新增对外错误协议。API 的通用 acquire 直接调用 store.acquire，避免把普通 get 误用成执行授权。

`committed`、`released` 和 `unknown` 不会被转换回 acquired。反向图片和 Worker 恢复在需要观察 terminal 事实时仍使用 get，并按原有未知执行语义停止 provider/外部调用；手动重试由上层生成新的逻辑 key，不重新激活旧 association。

### 3. 后置 Task 绑定是受控事实更新

pipeline 在叶子 Task 持久化前不能写入 task 外键，因此初始 request 的 `task_id` 为 `None`。可信 `bind_task` 只能对 acquired association 绑定一次当前 scope 的 Task，并同步更新 task_id 和 request fingerprint；之后执行器用带真实 Task ID 的相同事实读取同一 grant。已绑定、terminal 或跨 scope/改绑请求均拒绝。standalone Task 在 acquire 前已知 ID，直接使用完整指纹。

### 4. 历史行和崩溃窗口默认保守

migration 为旧 operation_grants 增加可空 source、units 和 request_fingerprint，以兼容已有数据库；旧行不能可靠恢复这些事实，因此 repository 发现任一缺失或摘要不一致就 fail-closed，不写猜测值。新行始终写入完整字段。reservation 与公共 grant 不共享事务的崩溃窗口仍由宿主 policy 的既有恢复流程负责，本 change 不自动 release 或重放未知副作用。

## Verification

- 内存测试覆盖相同事实复用、五类事实冲突、pipeline 后置绑定和三种 terminal/unknown 不可执行。
- PostgreSQL 测试覆盖持久字段、事实冲突、terminal acquire 拒绝、旧行缺失字段 fail-closed 和现有并发幂等路径。
- 运行相关 API、Agent、Worker、反向图片测试，以及严格 OpenSpec、compileall 和 diff 检查。
