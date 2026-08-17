## Purpose

为公共 operation policy 提供可审计的 grant association 请求事实校验和不可执行终态门禁，使宿主可以安全实现自己的计量与恢复，而不把商业规则耦合进核心。

## ADDED Requirements

### Requirement: grant association 必须绑定完整服务端请求事实

系统 MUST 为每个 `OperationRequest` 生成覆盖 `resource_id`、`task_id`、`source`、`units` 和可选 `input_digest` 的稳定请求指纹。内存和 PostgreSQL association store 命中相同 `scope`、`operation`、幂等键时，MUST 同时比较这些原始事实和指纹；任何事实冲突 MUST 使用既有 `operation_policy_unavailable` 语义 fail-closed，且不得返回旧 grant 或创建新 grant。客户端提交的 scope、user、grant、quota 或其他身份字段 MUST NOT 改变可信 request。

#### Scenario: 相同事实幂等复用

- **WHEN** 同一 scope、operation 和幂等键再次携带完全相同的资源、Task、source、units 和 input digest
- **THEN** store 返回原 association 和 grant
- **AND** policy 不会被重复 acquire，且请求指纹保持不变

#### Scenario: 请求事实冲突拒绝

- **WHEN** 同一幂等键的 resource、Task、source、units 或 input digest 任一不同
- **THEN** store 返回 `operation_policy_unavailable`
- **AND** 不复用旧 grant、不新增 association，且不进入真实副作用

#### Scenario: 历史行缺少可信事实

- **WHEN** PostgreSQL 命中一条缺少 source、units 或 request fingerprint 的历史 grant 行
- **THEN** store 返回 `operation_policy_unavailable`
- **AND** 不以 NULL、默认值或客户端输入猜测原始请求

### Requirement: 只有 acquired association 可以作为新的执行授权

`acquire` MUST 只返回状态为 `acquired` 的 association。`committed`、`released` 和 `unknown` MUST 被视为不可执行终态；它们可以由 `get` 返回给恢复或审计路径，但 MUST NOT 作为新的执行 grant 返回、重新预占或自动重放外部操作。状态转换 MUST 保持既有幂等语义，不能把 terminal/unknown 改回 acquired。

#### Scenario: terminal grant 不可重用

- **WHEN** 幂等命中 `committed`、`released` 或 `unknown` association 后再次 acquire
- **THEN** store 返回 `operation_policy_unavailable`
- **AND** 不调用 provider、不写入上传/删除副作用，也不创建替代 grant

#### Scenario: Worker 恢复观察 unknown

- **WHEN** Worker 恢复读取同一事实的 `unknown` association
- **THEN** get 只返回未知事实供恢复收束
- **AND** 恢复不得 release、重新 acquire 或自动发起同一外部调用

### Requirement: 后置 Task 绑定必须同步可信指纹

pipeline 在 acquire 时尚无叶子 Task 的情况下 MAY 使用 `task_id=None`，但可信 `bind_task` MUST 只允许 acquired association 绑定同 scope 的真实 Task，并同步更新持久 task_id 和 request fingerprint。绑定后的读取 MUST 使用真实 Task ID 命中同一 grant；重复改绑、跨 scope Task、terminal association 或事实冲突 MUST fail-closed。

#### Scenario: pipeline 绑定后恢复执行

- **WHEN** pipeline grant 先以无 Task request acquire，随后服务端创建并绑定真实 Task
- **THEN** association 持久化真实 Task ID 和对应的新指纹
- **AND** 执行器用真实 Task ID 重复读取时复用同一 grant

#### Scenario: standalone Task 使用完整事实

- **WHEN** standalone Agent 在创建 Task 后 acquire
- **THEN** request 直接包含 Task、目标图片和 input digest 的完整事实
- **AND** 任务执行与恢复不接受缺失或伪造的关联字段

### Requirement: 所有真实副作用路径必须使用可信命中契约

普通上传、合集导入、图片删除、pipeline/standalone Agent、反向图片 provider 以及 Worker 恢复 MUST 通过上述 association store/gateway 契约命中 grant。相同逻辑请求的自动恢复 MUST 复用相同事实；手动重试 MUST 使用新逻辑键。缓存命中和明确禁止的路径 MUST 不创建新的 provider grant。

#### Scenario: 上传与删除不接受 terminal grant

- **WHEN** API 上传或删除命中事实冲突或不可执行终态
- **THEN** API 沿用稳定 operation policy 错误
- **AND** 不写文件、Meme 或删除副作用

#### Scenario: 反向图片和 Agent 恢复不重放

- **WHEN** provider 请求或 Agent Task 恢复读取已计量、released 或 unknown grant
- **THEN** 路径沿用既有未知/降级语义并停止外部调用
- **AND** 不通过新 request id、空 task 或伪造 grant 绕过关联校验
