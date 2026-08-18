## ADDED Requirements

### Requirement: 图片处理状态必须公开条件性自动重命名阶段
图片处理 Job 状态 MUST 返回冻结的 `auto_name`、`auto_rename` 阶段状态及对应的 `image_auto_rename` 叶子 `task_id`（如有）。该阶段 MUST 支持 `queued`、`running`、`succeeded`、`failed`、`blocked`、`unknown_execution`、`skipped` 与 `warning`；`skipped` 时 MUST 没有叶子 Task，`warning` 时对应叶子 Task MUST 如实为 `failed`。只有可降级命名错误可以映射为 `warning`，目标或执行身份失效 MUST 使用停止 Job 的失败状态。`image_auto_rename` Task 本身 MUST 继续遵守普通 Task 的 `queued`、`running`、`succeeded`、`failed` 状态机，不得使用 `skipped` 或 `warning` 伪装其执行事实。

#### Scenario: 查询未启用自动重命名的 Job
- **WHEN** 客户端查询冻结 `auto_name=false` 的图片处理 Job
- **THEN** 响应包含 `auto_rename=skipped` 且没有该阶段的叶子 `task_id`

#### Scenario: 查询可降级失败的自动重命名 Task
- **WHEN** 自动重命名因可降级命名错误失败且 Job 已继续处理文本 embedding
- **THEN** Job 响应包含 `auto_rename=warning`，叶子 Task 响应包含 `status=failed` 和稳定错误

### Requirement: Job 必须公开非阻塞警告而不改变普通任务终态
图片处理 Job MUST 返回稳定的 `has_warnings` 布尔值和有限的结构化 warning 摘要；没有警告时 MUST 返回 `false` 和空摘要。只有可降级自动重命名失败是非阻塞 warning，若其他核心阶段有效，Job MUST 可以进入 `succeeded`；目标或执行身份失效不得被 warning 掩盖。warning MUST 标识阶段、稳定错误码、有限消息和可恢复性，不得包含路径、供应商密钥、内部 prompt 或其他 scope 信息；Job 和叶子 Task 的历史状态不得因查询而改写。

#### Scenario: 查询带警告的成功 Job
- **WHEN** 自动重命名失败而视觉、Agent 和文本 embedding 均成功
- **THEN** Job 返回 `status=succeeded`、`has_warnings=true` 及自动重命名 warning 摘要
- **AND** 对应 `image_auto_rename` Task 仍返回 `failed`

#### Scenario: 查询无警告的成功 Job
- **WHEN** Job 的全部必需阶段成功且可选阶段成功或被跳过
- **THEN** Job 返回 `status=succeeded`、`has_warnings=false` 和空 warning 摘要

#### Scenario: 查询不可降级的自动重命名失败
- **WHEN** 自动重命名因目标或执行身份失效而停止 Job
- **THEN** Job 返回失败、阻止或未知执行状态及稳定诊断，`has_warnings=false`
- **AND** 响应不得暗示文本 embedding 已继续执行

### Requirement: 自动重命名 Task 必须遵守图片阶段重试边界
`image_auto_rename` MUST 被视为图片阶段 Task。通用 Task 重试接口遇到该类型 MUST 返回稳定拒绝结果；受限图片阶段提交入口 MUST 根据当前 scope、Meme 和当前输入创建或复用独立 Task，终态 Task 的重试 MUST 创建新的逻辑 Task。独立重试状态 MUST 明确其不属于原 Job，不得唤醒、改写或重新收束原 Job，也不得自动创建其他阶段 Task。

#### Scenario: 查询独立自动重命名重试
- **WHEN** 用户通过受限图片阶段入口提交自动重命名重试
- **THEN** 任务状态将其标识为独立的 `image_auto_rename` Task，并公开新的 Task 标识或兼容活动 Task 标识
- **AND** 原 Job 的阶段、warning 和终态保持不变

#### Scenario: 拒绝从通用入口重试
- **WHEN** 用户对失败的 `image_auto_rename` Task 调用通用 Task 重试接口
- **THEN** 系统返回稳定拒绝错误，且任务列表中不出现脱离图片阶段约束的新 Task

### Requirement: 历史三阶段 Job 必须保持可查询
缺失 `auto_name` 和 `auto_rename` 阶段的历史图片处理 Job MUST 按 `auto_name=false` 与 `auto_rename=skipped` 读取，同时保留原 Job、三个既有阶段和叶子 Task 的历史事实。兼容读取不得创建自动重命名 Task、改变 Job 终态或把历史数据回写成一次新业务选择。

#### Scenario: 查询迁移前已完成 Job
- **WHEN** 客户端查询一个没有自动重命名字段的历史三阶段 Job
- **THEN** 系统返回兼容的 `auto_name=false` 和 `auto_rename=skipped`
- **AND** 原 Job 的终态、既有阶段和 Task 标识保持不变
