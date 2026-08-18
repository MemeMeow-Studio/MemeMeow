## Purpose

为上传与图片库补齐提供统一、可冻结且可审计的图片处理选项契约，并定义可选自动重命名阶段在安全并发、失败降级、任务观测和独立恢复方面的行为边界。

## ADDED Requirements

### Requirement: 图片处理选项必须在 Job revision 中冻结
系统 MUST 将规范化的 `reverse_image_policy` 与 `auto_name` 作为每个图片处理 Job revision 的不可变业务选项持久化。`reverse_image_policy` 只允许 `forbid` 或 `auto`，`auto_name` 只允许布尔值；客户端缺省、旧客户端未提交或历史 Job 缺失字段时，系统 MUST 分别按 `forbid` 和 `false` 处理。`auto_name` MUST 独立于 Agent 配置指纹，不得因是否自动重命名而改变 Agent 语境产物的配置身份。终态成功 Job 只有在两项选项兼容且三个核心阶段的当前产物仍有效时才可复用。

#### Scenario: 缺省选项采用安全值
- **WHEN** 客户端创建图片处理 Job 时未提交任一处理选项
- **THEN** 系统将该 Job revision 的 `reverse_image_policy` 冻结为 `forbid`，将 `auto_name` 冻结为 `false`

#### Scenario: 相同选项复用活动 Job
- **WHEN** 同一图片版本和处理配置已有活动 Job，且新请求的两个规范化处理选项均与其一致
- **THEN** 系统复用该活动 Job，不创建重复 Job 或重复叶子 Task

#### Scenario: 活动 Job 的处理选项不兼容
- **WHEN** 同一图片版本和处理配置已有活动 Job，但新请求的 `reverse_image_policy` 或 `auto_name` 与其不同
- **THEN** 系统返回稳定的处理选项冲突结果
- **AND** 系统不得修改既有 Job 的冻结选项或并行创建会覆盖相同业务产物的 Job

#### Scenario: 成功 Job 的核心产物已经过期
- **WHEN** 处理选项相同的历史成功 Job 仍存在，但其当前视觉、Agent 或文本产物已经缺失或过期
- **THEN** 系统不得把该 Job 复用为当前就绪结果，而应创建或复用从首个失效核心阶段恢复的新 revision

### Requirement: 联网策略必须表达授权而非执行承诺
`reverse_image_policy=auto` MUST 仅表示允许 Agent 在本次语境任务中按需使用已配置的第三方反向图片检索，不得表示系统必然发起联网请求；`forbid` MUST 禁止该任务使用反向图片检索。系统 MUST 在接受请求前校验 `auto` 所需服务是否可用；不可用时 MUST 返回稳定错误且不得创建部分 Job，`forbid` 请求仍 MUST 可执行。

#### Scenario: Agent 无需联网即可完成
- **WHEN** Job 冻结为 `reverse_image_policy=auto`，但 Agent 根据当前输入无需反向图片检索
- **THEN** Agent 可以在不发起第三方检索的情况下完成，系统不得把“未联网”视为失败

#### Scenario: 联网服务不可用
- **WHEN** 客户端选择 `auto`，但第三方反向图片检索服务未配置或当前不可用
- **THEN** 系统在创建 Job 前拒绝请求并返回稳定的服务不可用错误
- **AND** 相同条件下的 `forbid` 请求不受影响

### Requirement: 自动重命名必须是条件性的独立处理阶段
图片处理 Job MUST 按“视觉向量、Agent 语境、可选自动重命名、文本 embedding”的顺序推进；三个核心阶段的相对顺序保持不变，既有三阶段契约中的 Agent 与文本阶段之间允许插入该可选阶段。`auto_name=false` 时，`auto_rename` 阶段 MUST 记为 `skipped` 且不得创建叶子 Task；`auto_name=true` 时，系统 MUST 在有效 Agent 语境产生后创建或复用类型为 `image_auto_rename` 的持久叶子 Task，并只在该阶段收束后处理当前 metadata hash 对应的文本 embedding。Agent 语境 Task MUST NOT 内联执行文件重命名，也 MUST NOT 以自身输入字段携带 `auto_name`。

#### Scenario: 未启用自动重命名
- **WHEN** Job 冻结的 `auto_name=false` 且 Agent 语境阶段成功
- **THEN** `auto_rename` 阶段进入 `skipped`，系统不创建 `image_auto_rename` Task，并继续文本 embedding

#### Scenario: 启用自动重命名
- **WHEN** Job 冻结的 `auto_name=true` 且 Agent 语境阶段成功
- **THEN** 系统创建或复用一个 `image_auto_rename` 叶子 Task
- **AND** 系统在该 Task 收束前不开始本 Job 的文本 embedding 阶段

### Requirement: 自动重命名必须由服务端派生并防止并发覆盖
每个 `image_auto_rename` Task MUST 绑定服务端解析的 Meme 标识、目标图片 SHA-256、预期源 storage key，以及用于命名的当前语境标题或等价输入指纹。目标文件名 MUST 由服务端根据已验证语境生成并经过文件名安全规则处理；客户端不得提交目标文件名、路径或覆盖策略。Task 提交副作用时 MUST 原子校验绑定的 scope、Meme、图片 SHA、预期源 storage key 和有效执行权；若用户已手动重命名，Task MUST 失败并保留当前文件名。目标名称已被其他图片占用时，系统 MUST NOT 覆盖、交换或删除任何已有文件。若 Meme 被删除或移出 scope、图片 SHA 或语境指纹变化、执行权丢失或存储副作用无法确认，系统 MUST 将其视为不可继续的目标或执行身份失效，而不是可降级命名 warning。

#### Scenario: 自动重命名成功
- **WHEN** Task 的 Meme、图片 SHA、源 storage key 和语境输入均仍匹配，且安全目标名未被占用
- **THEN** 系统原子地更新该 Meme 的文件名与 storage key，同时保留不可变 Meme 标识和图片内容

#### Scenario: 用户先完成手动重命名
- **WHEN** 自动重命名排队期间，用户已将同一 Meme 从 Task 绑定的源 storage key 手动重命名
- **THEN** 自动重命名 Task 以稳定的目标已变化错误失败
- **AND** 系统保留用户的当前文件名，不使用过期结果覆盖它

#### Scenario: 自动目标名称冲突
- **WHEN** 服务端派生的安全目标名称已属于当前 scope 中的其他图片
- **THEN** 自动重命名 Task 以稳定的名称冲突错误失败
- **AND** 两张图片及其数据库记录均保持原状

#### Scenario: 图片或语境在执行前变化
- **WHEN** Task 执行或提交副作用时发现 Meme 已删除、scope 不匹配、图片 SHA 或语境指纹已变化
- **THEN** Task 以稳定的目标变化错误失败，Job 停止且不创建文本 embedding Task

#### Scenario: 自动重命名执行权失效
- **WHEN** Task 提交副作用时 claim 已丢失，或系统无法确认存储操作是否完成
- **THEN** Task 以稳定的执行身份或未知执行错误失败，Job 不把它降级为 warning，也不继续后续阶段

### Requirement: 可降级的自动重命名失败必须成为 Job 警告
`image_auto_rename` Task 因缺少可用标题、候选名称非法、目标名称冲突或预期 source key 已被同一图片的手动重命名替换而失败时，Task MUST 如实进入 `failed` 终态，`auto_rename` 阶段 MUST 进入非阻塞 `warning`，图片处理 Job MUST 公开 `has_warnings=true` 和有限的结构化警告。系统 MUST 保留当前文件名，基于失败收束后的当前 storage key 重新计算 metadata hash，并继续必要的文本 embedding；若其余核心阶段有效，Job MUST 可以进入 `succeeded`。图片身份、SHA、scope、语境指纹、claim 或存储副作用确认失效不属于可降级错误，MUST 按核心阶段的失败、阻止或未知执行规则停止 Job。系统不得把自动重命名失败伪装成 Task 成功，也不得把仍可用的语境和向量伪装成整体失败。

#### Scenario: 可降级自动重命名失败后继续文本 embedding
- **WHEN** Agent 语境和目标身份仍有效，但 `image_auto_rename` Task 因标题不可用、名称非法、名称冲突或同图手动改名而结束
- **THEN** 叶子 Task 状态为 `failed`，阶段状态为 `warning`，Job 的 `has_warnings=true`
- **AND** 系统使用当前未改名 storage key 对应的 metadata hash 继续文本 embedding

#### Scenario: 带警告的 Job 完成
- **WHEN** 自动重命名失败，但视觉向量、Agent 语境和当前 metadata hash 的文本 embedding 均有效
- **THEN** Job 进入 `succeeded` 且保留可查询的重命名警告

#### Scenario: 不可降级错误停止 Job
- **WHEN** `image_auto_rename` Task 因图片 SHA、scope、语境指纹、claim 或存储副作用确认失效而失败
- **THEN** Job 按稳定的失败或未知执行状态停止，不创建文本 embedding Task，也不返回可继续的重命名 warning

### Requirement: 失败的自动重命名必须通过受限阶段入口独立重试
用户 MUST 能通过既有受限图片阶段提交契约为当前 Meme 显式创建或复用独立的 `image_auto_rename` Task；该 Task MUST 使用 `standalone` 模式、不得关联父 Job，并遵守图片阶段现有的 scope、目标、claim、去重和结果有效性边界。通用 Task 重试接口 MUST 拒绝该类型；重试终态 Task MUST 创建新的逻辑 Task，并重新绑定当前图片、storage key 和语境输入。独立自动重命名重试 MUST NOT 重新激活原 Job、修改原 Job 历史或自动创建文本 embedding Task；重命名成功导致 metadata hash 改变时，系统 MUST 将既有文本 embedding 视为过期。

#### Scenario: 从专用入口重试失败的自动重命名
- **WHEN** 用户通过受限图片阶段入口重试一个当前输入仍可命名的 Meme
- **THEN** 系统创建或复用独立的 `image_auto_rename` Task，不重跑视觉或 Agent 阶段
- **AND** 原 Job 及其 warning 保持历史事实

#### Scenario: 通用 Task 重试被拒绝
- **WHEN** 客户端通过通用 Task 重试接口提交 `image_auto_rename` Task
- **THEN** 系统返回稳定的拒绝结果，且不创建新 Task 或 Job revision
