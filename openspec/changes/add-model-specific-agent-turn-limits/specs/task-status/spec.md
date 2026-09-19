## MODIFIED Requirements

金额终止及金额读取失败规则仅适用于冻结为启用分析用量策略的任务。公共默认配置关闭提醒和金额终止；未启用及历史无策略任务保留现有执行行为和活动观察字段。

金额终止、插件启动失败及用量读取失败的普通失败原因均以进程已经确认回收为前提。无法确认回收时 MUST 使用 `unknown_execution`；受保护诊断 MUST 同时保留原始触发原因、失败阶段和回收结果。

### Requirement: 语境研究任务必须提供可选的 Agent 活跃度摘要

任务状态接口 MUST 在可获得 OpenCode 会话数据时，为 `meme_context_generation` 任务返回 `agent_completed_turns`、`agent_turn_running` 和 `agent_last_activity_at`。`agent_completed_turns` MUST 表示已经结束的 Agent 步骤数量，`agent_turn_running` MUST 表示是否存在已经开始且尚未结束的步骤，`agent_last_activity_at` MUST 表示该 session 最近一次活动的 UTC 时间；这些字段 MUST NOT 被解释为任务完成百分比、剩余分析程度或金额。任务因达到分析程度终止边界进入终态时，公开错误码 MUST 为 `agent_maximum_analysis_depth_exceeded`，显示消息 MUST 为“超过最大分析程度”，不得直接返回提醒金额限额、终止金额限额或最终观测金额。

#### Scenario: Agent 已完成若干轮且新一轮正在执行
- **WHEN** 客户端查询一个具有 19 次步骤开始、18 次步骤结束及最近 part 更新时间的语境研究任务
- **THEN** 响应返回 `agent_completed_turns: 18`、`agent_turn_running: true` 和对应的 `agent_last_activity_at`

#### Scenario: Agent 当前没有未完成轮次
- **WHEN** 客户端查询一个步骤开始数与步骤结束数相同的语境研究任务
- **THEN** 响应返回相应的已完成轮次，并返回 `agent_turn_running: false`

#### Scenario: 分析程度终止后的公开状态
- **WHEN** Agent 任务因为达到终止金额限额而停止
- **THEN** 任务状态为 `failed`，详情返回 `agent_maximum_analysis_depth_exceeded` 和“超过最大分析程度”，且不向普通用户返回内部金额策略或最终观测金额

#### Scenario: 非语境研究任务
- **WHEN** 客户端查询缓存生成或其他不由 OpenCode Agent 执行的任务
- **THEN** 响应不提供 Agent 活跃度摘要

### Requirement: Agent 活跃度观测必须只读且可省略

系统 MUST 仅从 Agent 运行时已有的会话元数据计算活跃度，不得返回推理文本、工具参数、原始日志、消息正文、分析金额或内部金额限额。会话数据库不存在、繁忙、不可读、schema 不兼容或找不到任务 session 时，系统 MUST 省略 Agent 活跃度字段，并继续返回正常任务状态；活跃度观测失败不得改变或中断任务执行。Executor 为执行分析程度终止而读取累计金额属于独立的执行控制，该读取失败不得转换成普通活跃度字段缺失。

#### Scenario: OpenCode 会话数据库不可用
- **WHEN** 客户端查询任务时 OpenCode 会话数据库不存在、被锁定或无法按预期 schema 查询
- **THEN** 任务状态接口仍成功返回现有任务字段，且省略 Agent 活跃度字段

#### Scenario: 历史任务没有对应 session
- **WHEN** 客户端查询一个没有对应 OpenCode session 的语境研究任务
- **THEN** 任务状态接口正常返回任务信息，且不提供无法确认的轮次或最近活跃时间

#### Scenario: 查询任务列表
- **WHEN** 客户端查询包含多个语境研究任务的任务列表
- **THEN** 系统以有界的批量读取装配每个可匹配任务的活跃度，不因任务数量逐任务建立独立观测流程

#### Scenario: 活跃度观测失败不改变分析程度终止
- **WHEN** 页面读取轮次或最近活动时间失败，但 Executor 已确认累计金额达到终止金额限额
- **THEN** Executor 仍终止任务，公开状态仍为“超过最大分析程度”

#### Scenario: 执行控制用量读取失败
- **WHEN** Executor 无法读取执行终止判断所需的当前 session 累计金额
- **THEN** 任务进入 `failed`，记录稳定错误 `agent_analysis_usage_unavailable` 和明确失败阶段，不把该故障转换成活跃度字段缺失或无限分析程度
