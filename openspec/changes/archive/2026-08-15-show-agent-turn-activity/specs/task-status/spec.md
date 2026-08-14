## ADDED Requirements

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
