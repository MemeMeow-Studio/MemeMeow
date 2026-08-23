## Purpose

为耗时的索引生成、视觉处理和 Agent 研究操作提供统一、可轮询的持久任务契约，使前端能够展示进度和错误，并明确服务重启时的恢复与收束语义。
## Requirements
### Requirement: 系统必须为长任务返回任务标识
耗时操作 MUST 立即返回唯一任务标识和 `202` 状态，不得要求客户端保持原请求连接直到任务结束。短操作可以同步完成。

#### Scenario: 创建长任务
- **WHEN** 客户端提交缓存生成、图片处理或批量补齐任务
- **THEN** 系统返回唯一 `task_id`、初始状态 `queued` 和任务类型

### Requirement: 系统必须提供统一任务状态
任务状态接口 MUST 只返回当前 scope 的任务，并 MUST 返回任务类型、状态、进度（无法估计时可为 `null`）、当前消息、创建时间、完成时间和错误信息（如有）。任务状态 MUST 持久化到 PostgreSQL；任务只能从 `queued` 进入 `running`，再进入 `succeeded` 或 `failed`，租约到期后的重新认领不得被报告为第二个任务。

#### Scenario: 查询运行中任务
- **WHEN** 客户端查询当前 scope 中存在的运行中任务
- **THEN** 系统返回 `running` 状态及最新可用进度，不重复创建或执行第二份任务记录

#### Scenario: 查询未知任务
- **WHEN** 客户端查询不存在或不属于当前 scope 的 `task_id`
- **THEN** 系统返回 `404`，错误标识为 `task_not_found`，不泄露任务归属

### Requirement: 失败任务必须可诊断且不伪装成功
任务异常时 MUST 进入 `failed` 状态，返回稳定错误标识和面向用户的消息；已完成的部分结果 MUST 按各业务规格保留，不得将失败任务报告为成功。

#### Scenario: 执行过程失败
- **WHEN** 长任务中的外部模型调用或文件处理失败且无法继续
- **THEN** 系统记录失败状态，状态查询返回错误信息，且任务不再执行后续步骤

### Requirement: 服务重启必须恢复或收束未完成任务
任务事实 MUST 持久化到 PostgreSQL，而不是只存在于进程内存。服务或 Worker 重启后，`queued` 任务 MUST 保持可执行；失去有效执行租约的 `running` 任务 MUST 按重试策略被重新认领或进入可诊断的 `failed` 终态，系统不得永久报告一个无人执行的运行中任务。

#### Scenario: 服务在排队期间重启
- **WHEN** 服务重启且任务仍为 `queued`
- **THEN** 新 Worker 从 PostgreSQL 认领并继续执行同一 `task_id`

#### Scenario: 服务在任务执行期间重启
- **WHEN** 执行 `running` 任务的 Worker 退出且租约过期
- **THEN** 其他 Worker 重新认领同一任务，或在达到重试上限后将其标记为 `failed` 并记录稳定错误

### Requirement: 任务必须标识并发配置的生效状态
任务服务 MUST 能够区分当前有效的 Agent 并发配置与已保存但尚未重启生效的配置。任务详情和设置页可以展示配置版本或摘要，但 MUST NOT 暴露密钥或完整运行时机密。

#### Scenario: 任务使用当前有效配置
- **WHEN** `.env` 中已保存新的并发数量但服务尚未重启
- **THEN** 新提交的任务继续使用当前进程已加载的并发上限，并显示重启仍待完成

#### Scenario: 重启后任务使用新配置
- **WHEN** 服务已重启并加载新的有效并发数量
- **THEN** 新任务使用新上限，既有终态任务历史不被重写，排队和运行任务遵循既有恢复语义

### Requirement: 并行长任务必须受公平调度和背压保护
任务服务 MUST 对可并行的长任务施加显式全局并发上限、scope 级运行上限和排队背压，MUST 保证单一 scope 或任务类型不会无限占用所有执行资源，且 MUST 保持活动任务去重和 `queued -> running -> succeeded/failed` 状态语义。Agent lane 的跨 scope 选择 MUST 使用 PostgreSQL 持久公平状态和事务内公平 claim；公平状态不可用时 MUST 返回 `agent_fairness_unavailable`，不得退化为竞争式 claim。

#### Scenario: Agent 任务不得阻塞其他任务类型
- **WHEN** 语境生成 job 数量超过 Agent 并发上限，且存在 cache generation 或 metadata repair job
- **THEN** 超出上限的语境 job 保持 `queued`，其他任务类型仍能获得其保留的执行资源并更新状态

#### Scenario: 并行任务查询保持一致
- **WHEN** 多个 job 同时更新进度、失败或成功状态，客户端查询任务详情或列表
- **THEN** 每条任务记录只呈现自身的合法状态转换和结果，不出现跨任务覆盖、重复终态或丢失错误

#### Scenario: 服务重启收束并行执行
- **WHEN** 服务在多个语境子进程运行期间重启或关闭
- **THEN** 所有无法证明仍受管理的运行任务都被标记为 `failed/task_interrupted`，并且不得因旧进程残留而重复消费同一活动任务

### Requirement: Agent 结果文件失败必须使用稳定错误标识
系统 MUST 在语境任务因结果文件缺失、不可读、JSON 无效或 schema 无效而失败时，记录对应的稳定错误标识和面向用户的错误消息；不得将未写入语境的任务标记为成功。

#### Scenario: 缺少结果文件
- **WHEN** Agent 会话结束且任务输出目录中不存在结果文件
- **THEN** 任务状态为 `failed`，错误标识为 `agent_result_file_missing` 或等价稳定标识

### Requirement: 语境研究任务必须提供可选的 Agent 活跃度摘要
任务状态接口 MUST 在可获得 OpenCode 会话数据时，为 `meme_context_generation` 任务返回 `agent_completed_turns`、`agent_turn_running` 和 `agent_last_activity_at`。`agent_completed_turns` MUST 表示已经结束的 Agent 步骤数量，`agent_turn_running` MUST 表示是否存在已开始但尚未结束的步骤，`agent_last_activity_at` MUST 表示该 session 最近一次活动的 UTC 时间；这些字段 MUST NOT 被解释为任务完成百分比。

#### Scenario: Agent 已完成若干轮且新一轮正在执行
- **WHEN** 客户端查询一个具有 19 次步骤开始、18 次步骤结束及最近 part 更新时间的语境研究任务
- **THEN** 响应返回 `agent_completed_turns: 18`、`agent_turn_running: true` 和对应的 `agent_last_activity_at`

#### Scenario: Agent 当前没有未完成轮次
- **WHEN** 客户端查询一个步骤开始数与步骤结束数相同的语境研究任务
- **THEN** 响应返回相应的已完成轮次，并返回 `agent_turn_running: false`

#### Scenario: 非语境研究任务
- **WHEN** 客户端查询缓存生成或其他不由 OpenCode Agent 执行的任务
- **THEN** 响应不提供 Agent 活跃度摘要

### Requirement: Agent 活跃度观测必须只读且可降级
系统 MUST 仅从 Agent 运行时已有的会话元数据计算活跃度，不得返回推理文本、工具参数、原始日志或消息正文。会话数据库不存在、繁忙、不可读、schema 不兼容或找不到任务 session 时，系统 MUST 省略 Agent 活跃度字段，并继续返回正常任务状态；观测失败不得改变或中断任务执行。

#### Scenario: OpenCode 会话数据库不可用
- **WHEN** 客户端查询任务时 OpenCode 会话数据库不存在、被锁定或无法按预期 schema 查询
- **THEN** 任务状态接口仍成功返回现有任务字段，且省略 Agent 活跃度字段

#### Scenario: 历史任务没有对应 session
- **WHEN** 客户端查询一个没有对应 OpenCode session 的语境研究任务
- **THEN** 任务状态接口正常返回任务信息，且不虚构零轮或最近活跃时间

#### Scenario: 查询任务列表
- **WHEN** 客户端查询包含多个语境研究任务的任务列表
- **THEN** 系统以有界的批量读取装配每个可匹配任务的活跃度，不因任务数量逐任务建立独立观测流程

### Requirement: 前端必须把 Agent 轮次展示为活跃度信号
处理任务界面 MUST 对具有活跃度摘要的语境研究任务展示已完成轮次、当前轮次状态和最近活跃时间，并 MUST 保留任务自身的状态与进度展示。界面 MUST NOT 将 Agent 轮次呈现为总轮次、完成比例或预计剩余工作量；缺少活跃度摘要时 MUST 保持现有任务界面可用且不显示空占位。

#### Scenario: 运行中的语境研究持续产生活动
- **WHEN** 任务轮询取得更大的 `agent_completed_turns` 或更新的 `agent_last_activity_at`
- **THEN** 任务列表与当前打开的任务详情在下一次轮询后更新对应活动信息

#### Scenario: 活跃度摘要不可用
- **WHEN** 任务响应没有 Agent 活跃度字段
- **THEN** 前端继续展示现有状态、阶段和进度，且不显示错误、零轮或空活动区域

#### Scenario: 活跃度长时间未更新
- **WHEN** 运行中任务的 `agent_last_activity_at` 长时间未变化
- **THEN** 前端如实展示最近活动距今时间，但不据此把任务标记为失败或卡死

### Requirement: 任务认领和去重必须支持并发进程
系统 MUST 使用数据库原子操作确保一个任务在任一时刻最多由一个有效 Worker 执行，并 MUST 在并发提交语义相同的活动任务时复用同一 `task_id`。每次认领 MUST 产生递增的 claim generation，所有进度、终态和业务副作用提交都 MUST 验证当前 claim；租约过期的旧 Worker 不得写回。Agent lane 的全局并发上限、scope 级运行上限、等待队列背压和公平状态 MUST 对所有应用进程共同生效。

#### Scenario: 两个 Worker 同时认领
- **WHEN** 多个 Worker 同时尝试认领同一排队任务
- **THEN** 只有一个 Worker 获得有效租约并执行该任务

#### Scenario: 过期 Worker 恢复执行
- **WHEN** 旧 Worker 的租约已过期且任务已被新 Worker 重新认领，旧 Worker 随后恢复并尝试写回
- **THEN** 系统依据 claim generation 拒绝旧 Worker 的进度、终态和业务副作用提交

#### Scenario: 并发提交同一图片语境任务
- **WHEN** 多个请求为同一 scope、同一 Meme 指纹同时提交活动语境任务
- **THEN** 系统只保留一个活动任务并向所有提交者返回同一 `task_id`

#### Scenario: Agent 队列达到上限
- **WHEN** 所有进程合计的 Agent 运行任务和排队任务达到配置上限
- **THEN** 新任务被明确拒绝或返回现有去重任务，不因增加应用进程而绕过背压

### Requirement: Agent lane 必须按持久 scope 公平调度
Agent lane MUST 在 lane advisory lock 事务内从当前可执行 scope 中选择最久未服务者，并在同一事务内分配全局 slot、更新 Task claim/lease 和推进公平序号。公平状态表缺失、不可读或不一致时，任务 MUST 保持可恢复的排队状态并记录稳定 `agent_fairness_unavailable`，不能由旧 scope-bound 竞争入口替代。

#### Scenario: 单一 scope 不得连续占满可用 slot
- **WHEN** scope A 持续提交大量 Agent 任务，scope B、C 也持续有 queued 任务，且全局 lane 存在可用 slot
- **THEN** 调度器按持久公平序号轮转 scope，且每个 scope 的 running 数量不超过 `MEMEMEOW_AGENT_SCOPE_CONCURRENCY`

#### Scenario: 公平状态不可用
- **WHEN** Agent claim 无法读取或更新 `task_lane_fairness`
- **THEN** Task 保持 `queued`，返回或记录 `agent_fairness_unavailable`，不创建 running claim 或 lane slot

### Requirement: 批次收束必须恰好触发一次
系统 MUST 将任务与批次关系持久化，并在一个批次的全部语境任务进入终态后只触发一次该批次的索引刷新收束操作。服务重启、任务复用或多个 Worker 并发检查不得导致遗漏或重复触发。

#### Scenario: 批次全部完成
- **WHEN** 一个批次关联的全部语境任务进入成功或失败终态
- **THEN** 系统为该批次创建且只创建一个索引刷新任务或等价收束记录

#### Scenario: 重启后恢复批次
- **WHEN** 服务在批次部分完成时重启
- **THEN** 新 Worker 根据数据库关系继续跟踪剩余任务，并在全部终态后完成一次收束

### Requirement: 系统必须公开视觉向量任务状态
视觉向量生成 MUST 作为独立长任务执行，并通过统一任务接口返回任务类型、状态、进度、稳定错误和有限结果。任务 MUST 绑定创建时的 scope、`meme_id`、图片 SHA-256、视觉模型和预处理版本。

#### Scenario: 查询视觉任务
- **WHEN** 客户端查询存在的视觉向量任务
- **THEN** 系统返回 `visual_embedding_generation` 类型及其当前状态、进度和诊断信息

#### Scenario: 视觉任务输入已过期
- **WHEN** 任务执行时目标图片不存在或 SHA-256 已变化
- **THEN** 任务以稳定的目标变化错误结束，不写入当前图片的视觉向量

### Requirement: 视觉任务成功后必须幂等提交 Agent 任务
系统 MUST 在同一图片版本的视觉向量成功持久化后创建或复用对应的 `meme_context_generation` 任务。重复执行视觉任务完成逻辑 MUST NOT 为相同图片指纹和 Agent 配置重复创建自动语境任务；视觉向量失败时 MUST NOT 创建该后续任务。

#### Scenario: 首次视觉任务成功
- **WHEN** 当前图片版本首次成功持久化视觉向量且尚无对应 Agent 任务或有效 Agent 语境
- **THEN** 系统创建一个可轮询的 Agent 语境任务

#### Scenario: 完成逻辑被重复执行
- **WHEN** 同一视觉任务因重试或重复完成路径再次尝试提交后续任务
- **THEN** 系统复用已有 Agent 任务或已有有效 Agent 语境，不产生重复研究任务

#### Scenario: 后续任务提交失败
- **WHEN** 系统无法在视觉完成路径中可靠创建或复用 Agent 任务
- **THEN** 视觉任务不得伪装为完整成功，并保留可显式重试的诊断

### Requirement: 重试必须限制在失败阶段
用户显式重试视觉、Agent 或文本索引任务时，系统 MUST 默认只重试所选失败阶段。视觉模型重建 MUST NOT 自动重跑已经成功的 Agent 语境；Agent 重试 MUST NOT 重跑有效视觉向量；文本索引重试 MUST NOT 重跑视觉或 Agent 阶段。图片内容指纹变化属于新处理版本，不适用旧版本的阶段复用。

#### Scenario: 重试 Agent 失败
- **WHEN** 图片视觉向量有效但 Agent 语境任务失败，用户重试该 Agent 任务
- **THEN** 系统复用有效视觉向量，只重新执行 Agent 阶段

#### Scenario: 重建视觉模型向量
- **WHEN** 部署方为已有图片生成新视觉模型的向量
- **THEN** 系统不自动作废或重跑已经成功的 Agent 语境和文本语义内容

#### Scenario: 图片内容发生变化
- **WHEN** 同一 Meme 的当前图片 SHA-256 不再等于旧任务和旧产物指纹
- **THEN** 系统为新图片版本创建新的必要处理任务，并拒绝复用旧内容指纹的视觉向量

### Requirement: Agent 成功后必须收敛文本索引刷新
系统 MUST 在 Agent 成功写入有效语境后使文本索引进入待刷新状态。单图处理 MUST 提交或复用文本索引生成任务；批量处理 MUST 在关联 Agent 任务进入终态后收敛为一次文本索引生成，不得因每张图片完成而并发重建同一文本索引。

#### Scenario: 单图 Agent 成功
- **WHEN** 单张图片的 Agent 语境任务成功写回当前图片版本
- **THEN** 系统提交或复用文本索引生成任务

#### Scenario: 批量 Agent 任务完成
- **WHEN** 同一上传批次的 Agent 任务分别进入成功或失败终态
- **THEN** 系统只提交一次文本索引生成任务，并只索引满足现有文本索引资格的成功语境
