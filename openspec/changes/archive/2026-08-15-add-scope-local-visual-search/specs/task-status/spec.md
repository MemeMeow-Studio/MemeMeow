## ADDED Requirements

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
