## Purpose

为同一张图片同时提供完整的顺序处理和用户明确选择的单阶段处理，使提交语义、任务归属、结果有效性与安全授权均可观察且不可相互冒充。

## ADDED Requirements

### Requirement: 图片处理提交必须区分完整 Job 和独立阶段任务
系统 MUST 将完整图片处理 Job 与独立阶段任务作为两种不同的持久化提交模式公开。上传、图片库一键处理和“完整重试” MUST 创建或复用完整 Job；完整 Job MUST 按视觉向量、Agent 语境、文本 embedding 的固定顺序协调其所需阶段，并沿用现有的产物有效性校验和失败停止规则。用户选择“仅重试阶段”时，系统 MUST 创建或复用仅对应所选阶段的独立 Task；该 Task MUST 不属于任何图片处理 Job。系统只可以复用同一独立阶段请求的活动 Task；用户重试终态独立 Task 时 MUST 创建新的逻辑 Task。提交响应 MUST 明确返回模式、所创建或复用的 Job 或 Task 标识及所选阶段（如适用）。

#### Scenario: 用户完整重试一张图片
- **WHEN** 当前 scope 的用户对一张图片请求完整重试
- **THEN** 系统创建或复用该图片版本的完整处理 Job，并按固定顺序协调该 Job 所需的阶段
- **AND** 仍有效的阶段产物可以被复用，但 Job 不得被降格为只执行用户所选的一个阶段

#### Scenario: 用户仅重试视觉向量
- **WHEN** 当前 scope 的用户请求仅重试 `visual_embedding_generation`
- **THEN** 系统返回一个无父 Job 关联的独立视觉 Task
- **AND** 系统不得因此创建或调度 Agent 语境或文本 embedding Task

#### Scenario: 用户仅重试 Agent 语境
- **WHEN** 当前 scope 的用户请求仅重试 `meme_context_generation`
- **THEN** 系统返回一个无父 Job 关联的独立 Agent Task
- **AND** 系统不得因此创建或调度视觉向量或文本 embedding Task

#### Scenario: 用户仅重试文本 embedding
- **WHEN** 当前 scope 的用户请求仅重试 `text_embedding_generation`
- **THEN** 系统返回一个无父 Job 关联的独立文本 embedding Task
- **AND** 系统不得因此创建或调度视觉向量或 Agent 语境 Task

### Requirement: 独立阶段任务必须保持阶段边界和结果有效性
独立阶段 Task MUST 只读取和写入该阶段定义的当前输入与产物，并在执行开始及结果写回前校验服务端解析的 scope、目标 Meme、当前图片 SHA-256、阶段配置和当前 claim。独立 Agent Task 成功写入新的有效语境后，系统 MUST 使不匹配该语境版本或 metadata hash 的文本向量失效或不可检索；这一来源失效标记是结果一致性副作用，不是文本 embedding 阶段的执行。系统 MUST NOT 因此自动提交文本 embedding Task、重新激活 Job 或唤醒既有 Job reconcile。独立阶段 Task 不得推进或改写既有完整 Job 的阶段历史；后续由用户显式提交的完整 Job 可以根据当前产物有效性观察并复用其结果。

#### Scenario: 独立 Agent 结果使文本向量过期
- **WHEN** 独立 Agent Task 成功写入与当前文本向量来源不同的语境
- **THEN** 系统将不再把旧文本向量作为当前可检索结果
- **AND** 系统不自动创建文本 embedding Task 或完整 Job

#### Scenario: 独立 Task 的目标在执行期间变化
- **WHEN** 独立阶段 Task 执行或写回时发现目标图片已删除、scope 不匹配或图片 SHA-256 已变化
- **THEN** Task 以稳定的目标变化诊断结束，且不得写入任何其他图片版本或 scope

### Requirement: 图片阶段任务的归属和去重必须持久化且不可由客户端伪造
系统 MUST 为每个新建图片阶段 Task 持久化其提交模式、阶段、服务端派生的 scope、目标 Meme、目标图片 SHA-256、阶段配置及可选完整 Job 关联。完整 Job 的叶子 Task 只能由图片处理编排控制面创建，且必须关联该 Job；独立阶段 Task 必须没有 Job 关联。服务 MUST 仅在 scope、图片版本、阶段、模式、配置以及 Agent 阶段的规范化反向图片策略均相同的活动请求之间复用执行身份；不同模式不得因 Task 类型相同而相互复用。客户端不得提交或覆盖 scope、Job 关联、Task 归属、图片路径、claim、grant、callback 标识或阶段状态。

#### Scenario: 并发独立阶段重试
- **WHEN** 当前 scope 并发提交针对同一图片版本、同一阶段和相同配置的独立阶段请求
- **THEN** 系统最多保留一个活动独立 Task，并向各调用方返回同一 Task 标识

#### Scenario: 独立请求与完整 Job 同时存在
- **WHEN** 同一图片版本已有活动完整 Job，用户又提交一个独立阶段请求
- **THEN** 系统依据两种不同提交模式分别处理其活动去重
- **AND** 系统不得把独立 Task 冒充为该 Job 的子任务，或把 Job 子任务作为独立重试的结果返回

### Requirement: 历史未归类图片阶段 Task 必须只读展示
系统 MUST 将无法由可信持久化关系判断提交来源的历史图片阶段 Task 标记为未归类历史诊断项，而非 `pipeline` 或 `standalone` 提交模式。此类 Task MUST 可在当前 scope 内查询其既有状态和诊断，但 MUST NOT 触发通用 Task 重试、阶段重试或完整 Job 重试；用户需要重新处理时，必须针对当前图片显式提交完整 Job 或独立阶段任务。

#### Scenario: 查询未归类历史图片任务
- **WHEN** 用户查询当前 scope 内缺少可信 Job 关联和提交来源的历史图片阶段 Task
- **THEN** 系统返回其未归类历史诊断状态
- **AND** 系统不得将其呈现或返回为用户手动发起的独立阶段任务

### Requirement: 独立 Agent 阶段必须使用既有受限操作安全边界
独立 Agent Task MUST 使用与 Job 所属 Agent Task 相同的 operation policy、可信 grant、反向图片策略、scope 事实来源、执行 attempt、服务间 callback 凭据验证和 callback fencing 约束。新的独立 Agent 逻辑 Task MUST 在没有可复用的同一活动独立 Task 后才获取新的 `analysis.agent` grant；重试终态独立 Agent Task MUST 创建新的逻辑 Task 并重新经过 policy。Agent callback MUST 由服务端验证其服务凭据、Task、scope、目标 SHA、attempt 和当前 claim，验证失败时不得访问 provider、usage、缓存或业务写入。视觉和文本 embedding 独立 Task MUST NOT 消耗 `analysis.agent` grant。

#### Scenario: 独立 Agent 操作被策略拒绝
- **WHEN** 独立 Agent 阶段无法获取 `analysis.agent` 操作许可
- **THEN** 系统返回或记录既有的稳定 policy 拒绝原因，且不得启动 Agent provider 或创建伪造成功结果

#### Scenario: 跨 scope 伪造独立阶段提交
- **WHEN** 客户端在独立阶段提交中携带其他 scope、其他图片的内部标识、绝对路径或 callback/grant 字段
- **THEN** 系统忽略或拒绝这些不可信字段，并只使用服务端从当前 scope 与目标 Meme 派生的输入

#### Scenario: 未认证或错配的独立 Agent callback
- **WHEN** Agent callback 缺少有效服务凭据，或其 Task、scope、目标 SHA、attempt 或 claim 与当前独立 Agent Task 不匹配
- **THEN** 系统在 provider、usage、缓存和业务写入前拒绝该 callback
