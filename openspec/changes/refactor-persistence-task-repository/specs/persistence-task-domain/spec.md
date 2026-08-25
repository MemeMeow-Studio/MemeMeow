## Purpose

为任务执行及其反向图片审计、内部 callback 事实提供一个受 scope 绑定、可 fencing、可幂等且失败关闭的持久化边界，保证 Worker 重启、并发请求和分页读取不改变既有安全事实。

## ADDED Requirements

### Requirement: Task facts remain scope-bound and fail closed

系统 MUST 在读取、提交、批次关联、claim、租约写回和分页任务时使用已绑定的 scope；任务操作不得读取、修改或返回其他 scope 的任务、批次、lane slot、fairness 或成员关系。缺失 scope、非法 owner/lane/capacity、无效状态转换、公平状态损坏或数据库事实不可用时 MUST 返回稳定错误并停止，不得退化到进程内 cursor 或竞争式 claim。

#### Scenario: Cross-scope task lookup is invisible

- **WHEN** 当前 scope 用另一 scope 的 task ID、batch ID 或 callback/usage 事实 ID 查询或修改
- **THEN** repository 按资源不存在或稳定绑定错误处理，且不得泄露另一 scope 的记录

#### Scenario: Invalid claim configuration is rejected

- **WHEN** claim owner 为空、lane 非法、capacity 无法规范化或公平表存在重复/负序号
- **THEN** 操作返回对应稳定错误码，不创建 claim、不推进 fairness、不抢占 lane slot

### Requirement: Task submission and lifecycle preserve idempotency and fencing

系统 MUST 保持活动 dedupe key 的 scope/type 组合幂等；并发唯一冲突 MUST 通过保存点恢复并读取权威任务，而不是生成第二个活动任务。claim MUST 原子递增 generation/attempt、写入 owner/lease、分配 lane slot 和公平序号；heartbeat、progress、终态、失败重试和 provenance 写回 MUST 同时验证当前 scope、owner、generation 和未过期 lease，旧 Worker 写回 MUST 被拒绝。

#### Scenario: Active task submission converges

- **WHEN** 同一 scope、task type 和 dedupe key 被重复提交
- **THEN** queued/running 活动任务被复用，payload 不产生第二个活动事实；唯一冲突无法解析时返回 `task_submit_conflict`

#### Scenario: Expired or stale worker cannot write

- **WHEN** Worker 使用旧 generation、错误 owner 或已过期 lease 写 heartbeat、失败、进度或成功结果
- **THEN** 写回返回未改变状态的拒绝结果，lane slot 和当前 claim 不被旧 Worker 清理或覆盖

#### Scenario: Lease recovery respects attempt terminal state

- **WHEN** running 任务 lease 过期且 attempt 尚未达到上限
- **THEN** 任务回到 queued、保留 `lease_expired` 错误历史并释放 slot；达到上限时进入 failed 并记录 `max_attempts_exceeded`

### Requirement: Fair claim and batch finalization are transactional

系统 MUST 在持久 lane lock 内按 scope fairness、scope capacity、available_at/created_at/id 稳定选择任务，并将 slot、claim 和 fairness 更新放在同一事务；slot 不足或数据库公平事实不可用时 MUST 不推进序号。批次 MUST 以 scope 绑定、成员关系幂等、封口后拒绝非法成员，并且只在所有成员进入终态后一次性推进 finalizer 状态和可选任务。

#### Scenario: Fair claim rotates eligible scopes

- **WHEN**多个 scope 在同一 lane 有 queued 任务且存在可用 slot
- **THEN** 系统按持久 `last_dispatch_sequence`、scope 创建时间和 scope ID 的稳定顺序选择，并原子写入新的 claim generation、lease、slot 和 fairness 序号

#### Scenario: Batch finalizer waits for active members

- **WHEN**已封口批次仍有 queued/running 成员
- **THEN** finalizer 不创建或复用索引任务，批次保持待处理状态；所有成员终态后才可幂等收束

### Requirement: Task listing uses stable scope-safe cursor pagination

系统 MUST 按 `updated_at` 降序和 task ID 降序返回当前 scope 任务，限制每页最多 100 条；cursor MUST 只解释当前 scope 中的记录，并返回稳定的下一页 cursor。无效或其他 scope cursor 不得扩大查询范围。

#### Scenario: Cursor page has deterministic continuation

- **WHEN**调用方按状态/类型筛选并使用上一页返回的 cursor
- **THEN** 下一页只返回排序键严格位于 cursor 之后的当前 scope 记录，不重复或跳过同一更新时间下的任务

### Requirement: Usage and callback facts are binding-safe and idempotent

反向图片 usage 事件和 Agent callback 事实 MUST 保存完整 scope、task、claim generation、attempt、operation、目标 SHA 和输入摘要绑定；request ID 或逻辑键重复提交 MUST 收敛到同一权威记录，改绑、历史重复、schema 缺失或不完整绑定 MUST fail-closed。已完成 usage/callback 事实 MUST 保持终态，不被普通重试覆盖；任务级审计只能返回脱敏摘要。内存 callback 夹具 MUST 与 PostgreSQL 的双索引和终态收束语义一致，但不得替代生产 PostgreSQL schema 门禁。

#### Scenario: Callback logical key wins over a new request ID

- **WHEN**同一完整 callback 绑定以不同 request ID 再次解析
- **THEN** 系统返回最初的权威事实；同一 request ID 尝试改绑到不同 task/generation/input 时返回 `callback_request_conflict`

#### Scenario: Missing callback schema stops execution

- **WHEN** callback repository 运行在非 PostgreSQL、缺少复合唯一索引或绑定列可空的数据库上
- **THEN** 返回 `callback_binding_schema_unavailable`，不创建 request 事实、不回退到 request-ID-only 语义

#### Scenario: Completed usage is immutable

- **WHEN**同一 usage request 已完成后再次 finish，或任务聚合跨 scope 请求
- **THEN** 已完成事件按幂等读取返回且不覆盖审计；聚合只计算当前 scope 的事件并隐藏图片身份和 provider secret
