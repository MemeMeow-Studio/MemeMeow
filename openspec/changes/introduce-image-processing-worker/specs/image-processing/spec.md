## Purpose

为每张已入库图片提供可恢复、可观察且 scope 隔离的处理链契约，使视觉向量、Agent 语境和文本语义向量能够按固定顺序完成，并允许上传与图片库补齐共用同一行为。

## ADDED Requirements

### Requirement: 系统必须为每个图片版本维护统一处理 job
系统 MUST 为当前 scope 中每个需要处理的 Meme 图片版本创建或复用一个持久化图片处理 job。job MUST 绑定 scope、不可变 Meme 标识、目标图片 SHA-256、规范化 `reverse_image_policy` 及影响阶段有效性的配置版本，并 MUST 公开总体状态、当前或最后阶段、每个阶段的状态和有限错误诊断。终态成功 job 只有在当前图片、当前 Agent 语境版本、反向图片策略和当前文本 metadata hash 均与其已验证产物匹配时才可复用；语境、策略或 metadata 变化 MUST 创建或复用新的处理 job revision，旧 job 保持历史终态。终态失败、`blocked` 或 `unknown_execution` job 不得由上传自动流程重新激活；只有用户显式重试或受控部署恢复可以创建新 revision。客户端不得把 scope、图片路径、重试模式或阶段状态作为创建 job 的可信授权事实。

#### Scenario: 并发请求处理同一图片版本
- **WHEN** 同一 scope 内多个上传请求或补齐请求同时针对同一 Meme、同一图片 SHA-256 和相同处理配置创建 job
- **THEN** 系统返回同一个活动 job 或已验证可复用的完成 job，且不并行执行重复的阶段副作用

#### Scenario: 查询其他 scope 的处理 job
- **WHEN** 客户端查询不属于当前 scope 的图片处理 job 标识
- **THEN** 系统返回与未知 job 相同的未找到结果，且不泄露其存在、阶段或错误信息

#### Scenario: 语境变化后重新处理
- **WHEN** 同一图片 SHA-256 的可 embedding 语境或 metadata hash 发生变化，且旧 job 已成功完成
- **THEN** 系统不复用旧 job 作为当前结果，而是创建或复用绑定新语境版本的 job revision，并从首个失效阶段继续

### Requirement: 图片处理必须按固定阶段顺序推进
系统 MUST 依次完成视觉向量、Agent 语境和单图文本 embedding 三个阶段，并 MUST 使用独立持久 Task 执行每个需要运行的阶段。图片处理 job MUST 保存或可查询对应的叶子 `task_id`；视觉、Agent 和文本 Task 不得直接创建或调度彼此，只有图片处理 Worker 可以创建、复用和观察叶子 Task 后推进 job。每个 Task 开始前及外部调用结果写回前 MUST 校验目标 Meme 仍属于 job 和 Task 的 scope、图片仍存在且 SHA-256 未变化；文本 Task 还 MUST 校验当前语境状态、metadata hash 和 embedding 模型版本未变化。系统 MUST 只把结果写入该目标图片版本，且阶段完成与后续 Task 可创建性必须持久化衔接。

#### Scenario: 三个阶段均成功
- **WHEN** 当前图片版本依次获得有效视觉向量、有效 Agent 语境和有效文本 embedding
- **THEN** job 进入成功终态，三个阶段均显示成功及对应叶子 `task_id`，文本 embedding 立即具备当前 scope 的搜索资格

#### Scenario: 目标图片在阶段间变化
- **WHEN** 任一阶段准备执行或提交结果时发现目标 Meme 已删除、已移出 job 的 scope 或图片 SHA-256 已变化
- **THEN** job 以稳定的目标变化错误结束，后续阶段不再执行，且不得写入新图片版本或其他 scope

### Requirement: 失败必须停止当前图片的后续阶段
任一阶段达到不可继续的失败终态时，系统 MUST 将图片处理 job 标记为失败或被阻止，并 MUST NOT 自动创建或执行该图片的后续阶段。已经成功提交的图片、阶段产物和其他图片的 job MUST 保留且继续独立运行。

#### Scenario: 视觉阶段失败
- **WHEN** 视觉服务不可用、输入无效或视觉结果无法安全写回
- **THEN** 图片处理 job 保留失败诊断，Agent 和文本 embedding 阶段均不执行，且已上传图片仍可访问

#### Scenario: Agent 阶段失败
- **WHEN** 视觉向量已成功但 Agent 调用、输出校验或写回失败
- **THEN** 视觉向量保留，文本 embedding 阶段不执行，且其他图片的处理不受影响

#### Scenario: Agent operation 被 policy 阻止
- **WHEN** `analysis.agent` acquire 返回 `operation_forbidden`、`operation_limit_exceeded` 或 `operation_policy_unavailable`
- **THEN** job 的 Agent 阶段进入 `blocked`，保存稳定原因和 policy 可选提供的 `retry_at`，不创建或启动 Agent Task
- **AND** `retry_at` 到达时不自动重试，其他图片的处理继续独立进行

### Requirement: 处理必须根据有效产物增量恢复
系统 MUST 依据当前图片 SHA-256、视觉模型与预处理版本、Agent 配置、`reverse_image_policy` 或结果版本、文本 metadata hash 和 embedding 模型版本判断每个阶段产物是否有效。创建、恢复或主动重新处理 job 时，系统 MUST 跳过连续的有效前置阶段，并从第一个缺失或过期阶段开始；不得因后续阶段失败而重复执行已验证有效的前置阶段。

#### Scenario: 仅缺失文本 embedding
- **WHEN** 当前图片版本的视觉向量和 Agent 语境有效，但对应 metadata hash 或 embedding 模型的文本向量缺失或过期
- **THEN** 系统只执行文本 embedding 阶段，并在成功后完成 job

#### Scenario: 用户重试失败 job
- **WHEN** 用户显式重试一个在 Agent 或文本 embedding 阶段为 `failed`、`blocked` 或 `unknown_execution` 的图片处理 job
- **THEN** 系统保留旧 job 的终态，创建并返回新的 job revision，从该失败、阻止、未知执行阶段或更早的首个已过期阶段恢复，不重新执行仍有效的前置阶段

### Requirement: 上传和图片库一键处理必须使用相同的逐图处理语义
图片字节和 Meme 记录持久化成功后，上传流程 MUST 使用本次规范化 `reverse_image_policy` 创建或复用该图片的活动或有效成功 job 并返回其标识，但 MUST NOT 等待任一模型阶段完成，也 MUST NOT 自动重启同一目标签名的终态失败、`blocked` 或 `unknown_execution` job。图片库一键处理 MUST 将本次规范化策略用于当前 scope 的每张图片并逐图执行显式重试语义：复用有效产物和同策略活动 Task，为需要恢复的终态 job 创建新的 revision，并从第一个失败、阻止、未知或过期阶段继续。不同策略的活动 job MUST 返回 `generation_policy_conflict`，不得通过并行创建第二个 Agent Task 解决。不同图片 MUST 独立成功、失败或被阻止，不得建立全库阶段屏障。

#### Scenario: 上传多张图片部分处理失败
- **WHEN** 同一批上传中的多张图片均已成功入库，而其中一张的处理阶段失败
- **THEN** 每张成功入库的图片各自返回处理 job 标识，失败图片的 job 记录诊断，其他图片继续处理且上传结果不被回滚

#### Scenario: 对图片库执行一键处理
- **WHEN** 客户端对当前 scope 的图片库发起一键处理，且其中包含历史失败、`blocked` 或 `unknown_execution` 的图片
- **THEN** 系统把该用户操作视为逐图显式重试，为需要恢复的图片创建新 job revision，同时复用仍有效的前置阶段和活动 Task
- **AND** 一张图片再次被 policy 阻止或处理失败不影响其他图片继续推进

#### Scenario: 上传不自动重启历史失败
- **WHEN** 上传自动流程遇到同一目标签名已有终态失败、`blocked` 或 `unknown_execution` job
- **THEN** 系统保留并返回既有失败诊断，不创建新的重试 revision，也不启动新的 Agent Task

### Requirement: 未完成处理必须在服务恢复后继续收束
图片处理 job 和阶段事实 MUST 持久化，不得只依赖请求进程内存。服务或执行进程重启后，未开始或失去有效执行权的 job MUST 根据恢复策略继续、重新认领或以可诊断终态收束；同一阶段的过期执行者不得覆盖新的阶段状态或业务产物。

#### Scenario: 执行进程在 Agent 阶段退出
- **WHEN** 图片处理 job 的 Agent 阶段执行中断且其执行权过期
- **THEN** 恢复后的执行者只按该阶段的恢复策略继续或收束该 job，且旧执行者随后恢复时不得写回结果或推进文本 embedding

### Requirement: 外部阶段结果必须绑定执行尝试并安全收束
系统 MUST 在每次外部视觉、Agent 或 embedding 调用前，以对应叶子 Task 为逻辑执行身份，持久化不可猜测的执行尝试标识、scope、目标 SHA、阶段输入摘要和配置版本；Agent attempt 还 MUST 绑定冻结的 `reverse_image_policy`。结果只能在 `task_id`、外部 session 或 request 标识、执行尝试、目标输入和当前 claim 全部匹配时被采纳。无法证明调用未发生且无法验证已有结果时，叶子 Task MUST 以 `failed` 终态和稳定 `unknown_execution` 错误码收束，job 阶段进入 `unknown_execution`，不得自动重放或推进后续阶段。

#### Scenario: 旧 Agent 结果晚于新认领到达
- **WHEN** 旧 claim 发起的 Agent 调用在 job 被新 claim 认领后返回
- **THEN** 系统拒绝旧 attempt 的结果、进度和后续阶段推进，除非恢复流程显式验证该 attempt 的外部 session、输入摘要和目标版本

#### Scenario: 外部调用结果无法确认
- **WHEN** Worker 在外部调用后崩溃，恢复者无法确认调用是否发生或无法查询绑定 attempt 的结果
- **THEN** 阶段进入 `unknown_execution`，对客户端公开稳定诊断，且只有显式重试或受控部署恢复才能创建新的逻辑阶段

### Requirement: Agent 请求参数必须由服务端派生
图片处理 Worker MUST 仅根据持久化 job、`meme_context_generation` Task、当前 scope 的 Meme 记录、受控相对图片引用、job 冻结的 `reverse_image_policy` 和服务端固定的 skill/config 构造 Agent 请求。Agent 及其内部 callback 使用该持久 Agent `task_id` 作为不透明执行标识，由后端从 `Task.scope_id` 和有效 claim 反查 scope 与目标。客户端不得提供或覆盖 scope、绝对路径、路径遍历片段、prompt、skill、session、grant 或阶段状态；Agent 结果必须绑定 job、task、attempt、scope、策略和目标 SHA 后才能写回。

#### Scenario: 客户端伪造 Agent 输入
- **WHEN** 请求 payload 包含其他 scope、绝对路径、`..`、自定义 session 或 grant 标识
- **THEN** 系统忽略或拒绝这些字段，并仅使用服务端从当前 scope 和 job 派生的参数执行

### Requirement: Agent 阶段必须冻结反向图片策略
系统 MUST 只接受 `forbid` 和 `auto` 两种 `reverse_image_policy`，缺失或历史数据 MUST 按 `forbid`。规范化策略 MUST 持久化到图片处理 job revision 和 `meme_context_generation` Task，并进入 Agent 阶段有效性、活动去重、attempt 输入摘要、Task 结果和 Meme provenance。同一图片和 Agent 配置的同策略活动请求 MUST 复用，异策略活动请求 MUST 返回 `generation_policy_conflict`；用户显式重试终态 job 时可以为新 revision 选择新策略。

#### Scenario: 同策略请求复用活动 job
- **WHEN** 同一图片版本和 Agent 配置已有相同 `reverse_image_policy` 的活动 job 或 Agent Task
- **THEN** 系统复用该 job 或 Task，并保持原策略和 usage 审计归属

#### Scenario: 不同策略请求与活动 job 冲突
- **WHEN** 同一图片版本和 Agent 配置已有活动 job，但新请求选择不同 `reverse_image_policy`
- **THEN** 系统返回 `generation_policy_conflict`
- **AND** 不创建第二个可能覆盖同一语境的 Agent Task

#### Scenario: 显式重试切换策略
- **WHEN** 用户显式重试终态图片处理 job 并为新 revision 选择另一个合法策略
- **THEN** 系统把新策略冻结到新 job 和必要的新 Agent Task，从首个受策略变化影响的阶段继续
- **AND** 旧 job、Task、usage 摘要和 provenance 保持历史事实
