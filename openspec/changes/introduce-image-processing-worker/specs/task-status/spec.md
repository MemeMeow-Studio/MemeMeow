## MODIFIED Requirements

### Requirement: 系统必须提供统一任务状态
任务状态接口 MUST 只返回当前 scope 的任务或图片处理 job，并 MUST 返回类型、状态、进度（无法估计时可为 `null`）、当前消息、创建时间、完成时间和错误信息（如有）。图片处理 job 还 MUST 返回总体状态、当前或最后阶段、冻结的 `reverse_image_policy`，以及视觉、Agent 和文本 embedding 的有限阶段状态、诊断和对应叶子 `task_id`；阶段可以处于 `queued`、`running`、`succeeded`、`failed`、`blocked` 或 `unknown_execution`。`blocked` MUST 使用通用稳定原因，并可返回 policy 提供的可选 `retry_at`，但不得将它表示为已安排的自动重试。叶子 Task 继续通过统一任务接口提供各自进度、Agent 活跃度和错误；未知外部执行 MUST 表示为叶子 `Task.status=failed` 与 `error_code=unknown_execution`，只有图片处理阶段/job 使用 `unknown_execution` 状态。所有状态 MUST 持久化到 PostgreSQL；Task 只能从 `queued` 进入 `running`，再进入 `succeeded` 或 `failed`，图片处理 job 则可在阶段之间等待推进，租约到期后的重新认领不得被报告为第二个逻辑 Task 或 job。显式重试终态图片处理 job MUST 返回新的 job revision 标识，旧 job 不得被重新激活。

#### Scenario: 查询运行中图片处理 job
- **WHEN** 客户端查询当前 scope 中存在的运行中图片处理 job
- **THEN** 系统返回总体状态、当前阶段及最新可用诊断，不重复创建或执行第二份同目标 job

#### Scenario: 查询运行中任务
- **WHEN** 客户端查询当前 scope 中存在的运行中普通任务
- **THEN** 系统返回 `running` 状态及最新可用进度，不重复创建或执行第二份任务记录

#### Scenario: 查询未知任务
- **WHEN** 客户端查询不存在或不属于当前 scope 的 `task_id` 或图片处理 job 标识
- **THEN** 系统返回 `404`，错误标识为 `task_not_found`，不泄露任务或 job 归属

#### Scenario: 查询未知执行
- **WHEN** 客户端查询一个外部调用结果无法安全确认的图片处理阶段
- **THEN** 系统返回 `unknown_execution` 状态和稳定诊断，不将其报告为成功，也不自动推进后续阶段

#### Scenario: 查询被 operation policy 阻止的阶段
- **WHEN** 客户端查询因 operation policy 拒绝而停止的图片处理阶段
- **THEN** 系统返回 `blocked`、稳定原因和 policy 可选提供的 `retry_at`
- **AND** 不将提示时间表示为已调度的自动重试

### Requirement: 批量图片处理必须逐图隔离而非批次收束
系统 MUST 将批量上传和图片库一键处理中的每张图片作为独立图片处理 job 跟踪。系统 MUST NOT 以一批视觉或 Agent 任务的全部终态为条件自动提交 scope 级文本索引刷新；一张图片的失败、`blocked`、重试、服务恢复或完成不得阻塞、回滚或提前终结其他图片的 job。

#### Scenario: 同一批图片处于不同阶段
- **WHEN** 一次批量处理中的图片分别处于视觉执行、Agent 失败和文本 embedding 成功状态
- **THEN** 系统分别返回每张图片自己的 job 状态，成功图片可用于搜索，失败图片不触发全批失败或全库索引刷新

#### Scenario: 批量处理在重启后恢复
- **WHEN** 服务在一批图片部分完成时重启
- **THEN** 恢复后的执行者根据每张图片的持久化 job 和阶段状态独立继续或收束，不执行批次 finalizer

#### Scenario: 终态 job 显式重试
- **WHEN** 用户对 `failed`、`blocked` 或 `unknown_execution` 的图片处理 job 发起显式重试
- **THEN** 系统创建新的 job revision 并返回新标识，旧 job 保持终态，只有新 Agent Task 才重新进行计量授权

#### Scenario: 图片库一键处理重试终态 job
- **WHEN** 用户对图片库发起一键处理且库中包含失败、`blocked` 或 `unknown_execution` 的 job
- **THEN** 系统按图片创建新的必要 job revision，并让每张图片独立重新经过当前 operation policy
- **AND** 有效或正在活动的 job 不创建重复 revision 或重复叶子 Task

### Requirement: 重试必须限制在图片处理的失败阶段
用户显式重试 `failed`、`blocked` 或 `unknown_execution` 的图片处理 job 时，系统 MUST 创建新的 job revision，并默认从所选图片的失败、阻止、未知执行阶段或首个已过期阶段恢复。重试 MUST NOT 自动重跑已验证有效的前置阶段，也 MUST NOT 改变其他图片 job 的状态。图片内容指纹变化属于新处理版本，不适用旧版本的阶段复用。租约丢失或可证明未发生外部调用的暂态错误可以在同一逻辑阶段内自动恢复；不可证明的外部调用不得自动重放。

#### Scenario: 重试 Agent 阶段失败
- **WHEN** 图片视觉向量有效但 Agent 阶段为 `failed`、`blocked` 或 `unknown_execution`，用户重试该图片的处理 job
- **THEN** 系统复用有效视觉向量，只重新执行 Agent 阶段及其成功后必需的文本 embedding 阶段

#### Scenario: 重建视觉模型向量
- **WHEN** 部署方为已有图片生成新视觉模型的向量
- **THEN** 系统依据产物有效性重新处理必要阶段，不把仍有效的其它阶段错误标记为成功或失败

#### Scenario: 图片内容发生变化
- **WHEN** 同一 Meme 的当前图片 SHA-256 不再等于旧 job 和旧产物指纹
- **THEN** 系统为新图片版本创建新的必要处理 job，并拒绝复用旧内容指纹的视觉向量、Agent 语境或文本 embedding

## REMOVED Requirements

### Requirement: 批次收束必须恰好触发一次
**Reason**: 文本 embedding 已改为逐图增量生成，批量图片处理不再以 scope 级索引刷新作为收束动作。
**Migration**: 调用方改为轮询每张图片的处理 job；全库 `cache_generation` 仅通过显式维护入口创建。

### Requirement: 视觉任务成功后必须幂等提交 Agent 任务
**Reason**: 阶段推进责任转移到统一图片处理 job，避免视觉任务与 Agent 任务之间的隐式耦合。
**Migration**: 视觉 Task 仅提交其自身产物；图片处理 Worker 在确认其成功和产物有效后创建或复用独立 Agent Task。

### Requirement: Agent 成功后必须收敛文本索引刷新
**Reason**: Agent 结果的文本向量按单图增量生成，不再自动触发 scope 级全库索引刷新。
**Migration**: Agent Task 仅提交自身语境产物；图片处理 Worker 在确认其成功后创建或复用独立 `text_embedding_generation` Task。维护者需要全库重建时显式提交 `cache_generation`。
