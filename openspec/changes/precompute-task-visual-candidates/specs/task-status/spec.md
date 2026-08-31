## MODIFIED Requirements

### Requirement: 任务状态必须暴露脱敏的视觉 snapshot 摘要

`meme_context_generation` Task MUST 在 Agent 启动前记录视觉候选预计算状态。成功时任务详情和结果可返回 `protocol_version`、`snapshot_sha256`、`matched_at` 和 `candidate_count`；失败时返回稳定错误。公开 DTO MUST NOT 返回候选 context、物理路径、storage key、scope ID 或 callback 凭据。

#### Scenario: snapshot 已准备

- **WHEN** 任务完成视觉候选预计算但 Agent 尚未启动
- **THEN** 任务状态可观察到候选数量和 snapshot hash，且 Agent grant 尚未提交的窗口不会被显示为外部执行

#### Scenario: 预计算失败

- **WHEN** 视觉候选准备返回稳定错误
- **THEN** 任务失败/重试摘要保留该错误，不能伪装为 `unknown_execution` 或成功

### Requirement: claim 恢复必须保留同一视觉 snapshot

任务恢复 MUST 以 Task 输入和 snapshot hash 校验 attempt 绑定；缺失或损坏的 snapshot 不能被 Agent 端补交或静默重建。旧任务可由后端在 claim 前置阶段一次性迁移到 protocol v2，已完成任务不重跑。

#### Scenario: snapshot hash 不匹配

- **WHEN** resume attempt 读取的 snapshot canonical hash 与持久摘要不一致
- **THEN** 任务以稳定 `visual_match_snapshot_invalid` 错误失败，不进入 OpenCode
