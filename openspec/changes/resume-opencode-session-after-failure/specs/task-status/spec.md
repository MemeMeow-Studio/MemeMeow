## MODIFIED Requirements

### Requirement: 失败任务必须可诊断且不伪装成功

任务异常且已无安全的自动恢复路径时 MUST 最终进入 `failed` 状态，返回稳定错误标识和面向用户的消息；已完成的部分结果 MUST 按各业务规格保留，不得将失败任务报告为成功。对于满足 Agent 会话续跑契约的暂态失败，在恢复次数和累计时间预算未耗尽期间，任务 MAY 暂时保持 `queued`，表示 Worker 正在按退避策略续跑；该中间态不代表成功、`running` 或用户显式重试。任务详情 MUST 返回有限的 `resume_available` 摘要、session 关联状态和当前 attempt；对于不可安全重放或预算耗尽的失败，系统 MUST 明确返回不可续跑原因并最终收束。

#### Scenario: 执行过程失败
- **WHEN** 长任务中的外部模型调用或文件处理失败且无法继续
- **THEN** 系统记录失败状态，状态查询返回错误信息，且任务不再执行后续步骤

#### Scenario: Agent 失败但允许续跑
- **WHEN** Agent 已创建可验证 session，且失败属于允许恢复的暂态错误
- **THEN** 在恢复预算未耗尽时任务暂时为 `queued`，状态接口返回 `resume_available: true` 和有限的恢复摘要
- **AND** Worker 按退避策略从同一业务 Task 继续续跑，任务不会被客户端误报为成功或 `running`
- **AND** 只有恢复被禁止、预算或累计时间耗尽后，任务才最终为 `failed` 或 `unknown_execution`

#### Scenario: Agent 失败且禁止重放
- **WHEN** 外部调用是否发生、结果是否完整或目标输入是否有效无法确认
- **THEN** 任务返回稳定的不可续跑错误，并保留已完成的安全产物

### Requirement: 服务重启必须恢复或收束未完成任务

任务事实 MUST 持久化到 PostgreSQL，而不是只存在于进程内存。服务或 Worker 重启后，`queued` 任务 MUST 保持可执行；失去有效执行租约的 `running` 任务 MUST 按重试策略被重新认领或进入可诊断的 `failed` 终态。若 Agent session 已持久化且符合续跑契约，恢复器 MUST 能够按明确 session 继续同一逻辑工作；否则 MUST 收束为可诊断终态，系统不得永久报告一个无人执行的运行中任务。

#### Scenario: 服务在排队期间重启
- **WHEN** 服务重启且任务仍为 `queued`
- **THEN** 新 Worker 从 PostgreSQL 认领并继续执行同一 `task_id`

#### Scenario: 服务在任务执行期间重启
- **WHEN** 执行 `running` 任务的 Worker 退出且租约过期
- **THEN** 其他 Worker 重新认领同一任务，或在达到重试上限后将其标记为 `failed` 并记录稳定错误

#### Scenario: Agent 进程退出但 session 可验证
- **WHEN** Worker 重启后发现 Agent session、输入摘要和 attempt 事实完整，且失败属于可续跑错误
- **THEN** 恢复器按明确 session 继续当前逻辑任务，不创建重复业务任务或复用终态 executor ID

#### Scenario: Agent 外部执行无法确认
- **WHEN** Worker 重启后无法证明 Agent 外部调用是否发生或无法绑定 session
- **THEN** 任务以 `failed` 和 `unknown_execution` 或等价稳定诊断收束，不自动重放外部调用
