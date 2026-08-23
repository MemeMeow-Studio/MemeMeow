## Purpose

为共享 Agent lane 提供跨 scope、可恢复且可验证的公平任务分配，使多个 Worker 在全局并发受限时不会长期由单一 scope 占用执行机会。

## ADDED Requirements

### Requirement: Agent lane 必须使用持久化公平状态
系统 MUST 为每个受公平调度的 lane 和 scope 保存独立的公平状态，状态至少能够表示该 scope 最近一次成功获得调度机会的顺序。公平状态 MUST 位于共享持久存储中，不得只保存在单个 Worker 进程内；公平状态损坏或不可读时，Agent lane MUST fail-closed 并返回稳定的调度不可用错误，不得退化为无序竞争。

#### Scenario: Worker 重启后保留轮询位置
- **WHEN** Worker 重启且多个 scope 都有可执行的 Agent 任务
- **THEN** 新 Worker 读取持久化公平状态继续轮询，不因进程内存清空而让某个 scope 获得未声明的额外优先级

#### Scenario: 公平状态不可用
- **WHEN** 公平状态表不可读、存在重复主键或无法与 lane 状态一致提交
- **THEN** Agent 任务不进行无序 claim，调用方收到稳定的调度不可用错误，既有运行任务继续按租约和 fencing 收束

### Requirement: Agent claim 必须在同一事务内完成公平选择
系统 MUST 提供由全局 Worker 调度器调用的公平 claim 操作。一次成功 claim MUST 在同一数据库事务内完成 lane 互斥、可执行 scope 选择、scope 运行上限检查、全局 lane slot 分配、Task 状态变更、claim generation/lease 写入和公平状态推进。客户端字段和普通 payload MUST NOT 参与公平分组选择。

#### Scenario: 多 Worker 同时 claim 不重复占槽
- **WHEN** 多个 Worker 同时对同一 Agent lane 请求公平 claim
- **THEN** 只有获得数据库 lane 锁和有效 slot 的事务能够将 Task 改为 `running`，每个 Task 在任一时刻最多有一个有效 claim

#### Scenario: 全局 slot 已满
- **WHEN** Agent lane 没有可用全局 slot
- **THEN** 公平状态不推进，候选 Task 保持 `queued`，不创建额外运行任务

### Requirement: 可执行 scope 必须按轮询顺序获得机会
公平调度 MUST 在每次成功 claim 时从当前有可执行任务且未达到 scope 运行上限的 scope 中选择最久未获得调度机会的 scope；同一 scope 内的 Task MUST 按 `available_at`、`created_at`、`id` 的稳定顺序选择。选择成功后 MUST 原子推进该 scope 的公平序号。只要两个 scope 在连续选择时都保持可执行，后获得机会的 scope MUST NOT 被连续跳过；暂时没有任务、达到 scope 上限或候选行被锁定的 scope 可以被跳过。

#### Scenario: 多 scope 轮询
- **WHEN** scope A、B、C 都持续有 queued Agent Task，且都未达到 scope 运行上限
- **THEN** 成功 claim 的 scope 按持久序号轮转，不能因为 A 有更多 queued Task 而连续获得所有新 slot

#### Scenario: scope 达到运行上限
- **WHEN** scope A 已达到其用户级运行上限，scope B 仍有可执行任务
- **THEN** 调度器跳过 A 选择 B，A 的公平序号不因未成功 claim 而推进

### Requirement: 公平调度必须保持现有恢复和 fencing 语义
公平 claim MUST 继续使用 Task 的 scope 归属、lease、claim generation 和 lane slot fencing。租约过期的 Task 重新进入候选时，其公平机会以新的成功 claim 计算；旧 Worker 的进度、终态和业务副作用 MUST 继续被拒绝。公平状态推进失败时，Task 状态和 slot 分配 MUST 一并回滚。

#### Scenario: 过期任务重新进入轮询
- **WHEN** 一个 Agent Task 的旧租约过期并被恢复为 `queued`
- **THEN** 该 Task 重新参与公平选择，旧 Worker 不能借旧 claim 推进公平状态或写回业务结果

#### Scenario: 公平状态提交失败
- **WHEN** Task 已锁定但公平序号更新无法提交
- **THEN** 整个 claim 事务回滚，Task 保持可恢复的排队状态且 lane slot 不被泄漏
