## MODIFIED Requirements

### Requirement: 任务认领和去重必须支持并发进程
系统 MUST 使用数据库原子操作确保一个任务在任一时刻最多由一个有效 Worker 执行。通用任务服务和父 Job 内部创建叶子 Task 时，语义相同的重复内部提交 MUST 复用同一活动 `task_id`；用户发起的完整重试、修复或独立图片阶段处理不适用该复用规则，同一图片已有任何活动处理时 MUST 返回 `image_processing_active`。每次认领 MUST 产生递增的 claim generation，所有进度、终态和业务副作用提交都 MUST 验证当前 claim；租约过期的旧 Worker 不得写回。Agent lane 的全局并发上限、scope 级运行上限、资源池运行上限和公平状态 MUST 对所有应用进程共同生效。

#### Scenario: 两个 Worker 同时认领
- **WHEN** 多个 Worker 同时尝试认领同一排队任务
- **THEN** 只有一个 Worker 获得有效租约并执行该任务

#### Scenario: 过期 Worker 恢复执行
- **WHEN** 旧 Worker 的租约已过期且任务已被新 Worker 重新认领，旧 Worker 随后恢复并尝试写回
- **THEN** 系统依据 claim generation 拒绝旧 Worker 的进度、终态和业务副作用提交

#### Scenario: 父 Job 重复创建同一叶子 Task
- **WHEN** 同一父 Job 因重复 reconcile 为同一阶段提交语义相同的活动叶子 Task
- **THEN** 系统复用该父 Job 已有的活动叶子 Task，不重复获取 Agent grant 或执行外部调用

#### Scenario: 并发提交同一图片语境任务
- **WHEN** 父 Job 内部或通用任务服务为同一 scope、同一 Meme 指纹并发提交语义相同的活动语境任务
- **THEN** 系统只保留一个活动任务并向内部调用方返回同一 `task_id`
- **AND** 该复用不适用于用户再次发起的完整重试、修复或独立阶段处理

#### Scenario: 用户并发提交同一图片处理
- **WHEN** 多个用户请求同时为同一 scope、同一 Meme 当前图片提交完整重试、修复或独立阶段处理
- **THEN** 最多一个请求创建父 Job 或独立 Task
- **AND** 其他请求返回 `image_processing_active`，不得复用第一个请求创建的活动执行

#### Scenario: Agent 队列达到上限
- **WHEN** Agent 运行槽位已满但请求没有命中活动图片处理或通用任务去重
- **THEN** 已接受的新任务继续保持 `queued`，不因排队数量增加而拒绝，也不能通过增加应用进程突破运行槽位

### Requirement: Agent 成功后必须收敛文本向量刷新
Agent 成功写入有效语境后，系统 MUST 使不匹配新语境的文本向量不再作为当前结果。pipeline Agent Task MUST 只唤醒父 Job，由父 Job 根据创建时固定的阶段计划决定是否执行自动重命名和文本向量；standalone Agent Task MUST 不自动创建文本向量、自动重命名或父 Job。批量中的每张图片 MUST 各自服从其父 Job 计划，不得恢复按整个批次隐式触发共享文本向量刷新的旧链路。

#### Scenario: 单图 Agent 成功
- **WHEN** 单张图片的 pipeline Agent Task 成功写回当前图片版本
- **THEN** 系统唤醒父 Job，并仅执行该父 Job 计划内的后续阶段

#### Scenario: 批量 Agent 任务完成
- **WHEN** 批量请求创建的各图片 Agent Task 分别进入终态
- **THEN** 每个父 Job 独立按其固定计划继续或收束
- **AND** 系统不因批次完成隐式创建计划外的共享文本向量任务

#### Scenario: 独立 Agent 成功
- **WHEN** standalone Agent Task 成功写入新的有效语境
- **THEN** 不匹配该语境的旧文本向量不再作为当前结果
- **AND** 系统不自动创建文本向量 Task、自动重命名 Task 或父 Job

## REMOVED Requirements

### Requirement: 视觉任务成功后必须幂等提交 Agent 任务

**Reason**: 图片处理已由父 Job 的固定阶段计划统一推进。视觉 Task 本身自动提交 Agent 会绕过修复计划，使“只修复视觉”错误地产生后续任务。

**Migration**: pipeline 视觉 Task 完成后只唤醒父 Job，由父 Job 根据持久计划决定是否创建 Agent；standalone 视觉 Task 完成后不创建任何后续 Task。

### Requirement: 重试必须限制在失败阶段

**Reason**: 单阶段处理、强制完整重试和最小修复是三种不同的用户意图，不能再由“只重试失败阶段”一条规则共同表达。

**Migration**: 独立阶段处理仍只执行所选阶段；完整重试执行全部已启用阶段；修复由父 Job 创建时根据当前结果和依赖固定最小执行范围。

## ADDED Requirements

### Requirement: 父 Job 必须在创建时固定阶段执行范围
每个新图片处理父 Job MUST 持久保存本次处理方式、各阶段是否计划执行以及不执行原因。公开阶段项 MUST 至少返回 `stage`、`status`、`planned`、`skip_reason` 和可选 `task_id`。计划执行的阶段 MUST 使用 `planned=true`、`skip_reason=null`，从 `queued` 开始并创建新的叶子 Task；创建时确认无需执行或未启用的阶段 MUST 使用 `planned=false`、`status=skipped` 以及 `already_ready` 或 `disabled` 原因，且不得伪造成该 revision 已实际执行成功。服务重启、租约恢复和重复 reconcile MUST 继续使用同一份计划。

#### Scenario: 修复 Job 在服务重启后恢复
- **WHEN** 一个只计划执行自动重命名的修复 Job 在排队后发生服务重启
- **THEN** 新 Worker 继续只执行自动重命名
- **AND** 不重新判断并添加视觉、Agent 或文本向量阶段

#### Scenario: 查询本次未执行的阶段
- **WHEN** 客户端查询一个跳过已有就绪阶段的修复 Job
- **THEN** 响应明确区分本次计划执行、已执行和本次未执行的阶段
- **AND** 对本次未执行阶段返回服务端记录的有限原因

#### Scenario: 本次未执行阶段的依据随后失效
- **WHEN** 父 Job 完成前发现创建时标记为无需执行的阶段已不再匹配当前图片或输入
- **THEN** 系统以 `image_processing_plan_stale` 停止该 Job
- **AND** 不在运行时把该阶段加入计划，也不把不再有效的旧产物报告为当前结果

### Requirement: 运行时校验不得改变已固定的阶段计划
Worker MUST 对计划内阶段创建新的逻辑叶子 Task，并执行该阶段；即使执行前发现旧产物仍然有效，也 MUST NOT 将其作为本 revision 的成功结果直接复用。运行时校验 MUST 继续核对 scope、目标图片、SHA、配置、claim 和输入版本，以阻止目标变化、越权或过期执行者写回，但这些安全校验不得把计划内阶段变成跳过。计划内已经关联 `queued` 或 `running` 叶子 Task 时，重启后的 reconcile MUST 继续等待或恢复同一 Task，不得创建第二个 Task。

#### Scenario: 完整重试命中已有有效产物
- **WHEN** 完整重试 Job 运行到一个已有有效产物的计划内阶段
- **THEN** Worker 仍创建并执行新的叶子 Task
- **AND** 该阶段只有在新 Task 成功后才标记为成功

#### Scenario: 计划内阶段执行前图片发生变化
- **WHEN** Worker 执行计划内阶段前发现图片已删除或 SHA 已变化
- **THEN** 系统以稳定的目标变化原因停止该 Job
- **AND** 不把旧产物复用为新 revision 的成功结果，也不向变化后的图片写回

#### Scenario: 重启后计划内叶子 Task 仍在活动
- **WHEN** Worker 重启后发现计划内阶段已经关联一个 `queued` 或 `running` 叶子 Task
- **THEN** 系统继续恢复或观察同一 Task
- **AND** 不创建第二个逻辑 Task、Agent grant 或外部执行

### Requirement: 图片重新处理必须区分完整重试、修复和独立阶段
用户选择完整重试时，系统 MUST 创建新的父 Job revision，并执行全部已启用阶段。用户选择修复时，系统 MUST 按父 Job 创建时固定的当前结果和依赖范围执行。用户选择独立阶段处理时，系统 MUST 只执行所选阶段，且不得创建父 Job 或其他阶段。图片内容指纹变化属于新处理版本，不得复用旧图片版本的产物或执行身份。

#### Scenario: 重试终态父 Job
- **WHEN** 用户通过父 Job retry 入口重试一个允许重试的终态 Job
- **THEN** 系统创建新的完整重试 revision，并计划执行全部已启用阶段
- **AND** 旧 Job、阶段和 Task 历史保持不变

#### Scenario: 独立处理 Agent 阶段
- **WHEN** 用户只提交当前图片的独立 Agent 阶段处理
- **THEN** 系统只创建独立 Agent Task
- **AND** 不重跑视觉或自动创建自动重命名和文本向量 Task

#### Scenario: 修复 Agent 未就绪
- **WHEN** 修复计划确认 Agent 未就绪而视觉结果仍有效
- **THEN** 系统不执行视觉，并执行 Agent 及依赖其新结果的已启用后续阶段

#### Scenario: 图片内容发生变化
- **WHEN** 同一 Meme 的当前图片 SHA-256 不再等于旧 Job、Task 和产物指纹
- **THEN** 系统为新图片版本创建新的必要处理身份
- **AND** 拒绝复用旧内容指纹的产物或活动执行

### Requirement: 视觉阶段完成后的推进必须服从父 Job 计划
视觉 Task 成功只表示当前视觉阶段产物已经写入。pipeline Task MUST 唤醒其父 Job，由父 Job 根据固定计划决定是否创建 Agent；standalone 视觉 Task MUST 不创建任何后续阶段。视觉失败时父 Job MUST 按既有失败停止规则收束。视觉结果可以是后续 Agent 执行的前置条件，但重新生成视觉结果本身 MUST NOT 自动使仍匹配当前图片和配置的 Agent 语境或文本语义结果失效。

#### Scenario: 普通处理计划包含 Agent
- **WHEN** pipeline 视觉 Task 成功，且父 Job 的下一计划阶段是 Agent
- **THEN** 系统唤醒父 Job 并由父 Job 创建或恢复 Agent Task

#### Scenario: 修复计划只包含视觉
- **WHEN** 只修复视觉的父 Job 完成视觉阶段
- **THEN** 父 Job 按既定计划收束
- **AND** 不创建 Agent、自动重命名或文本向量 Task

#### Scenario: 独立视觉 Task 成功
- **WHEN** standalone 视觉 Task 成功写入当前图片的视觉结果
- **THEN** 系统不创建或唤醒父 Job
- **AND** 不创建其他阶段 Task

### Requirement: 同一图片的用户处理请求必须互斥
同一 scope、同一 Meme 的当前图片只要存在任意 `queued` 或 `running` 的图片处理父 Job、父 Job 叶子 Task、独立阶段 Task 或可可靠绑定到该图片的历史未归类图片阶段 Task，新的完整重试、修复或独立阶段处理请求 MUST 返回 `image_processing_active`，不得创建或复用新的父 Job、Task、Agent grant 或外部执行。`succeeded`、`failed`、`blocked`、`unknown_execution`、取消或其他终态记录 MUST NOT 单独阻止新的合法请求。

活动检查和新父 Job 或独立 Task 的创建 MUST 在同一图片级数据库互斥边界内完成，保证并发请求最多只有一个成功创建执行身份。父 Job 内部创建自己的叶子 Task 不属于新的用户请求，不得被自身的活动状态阻止。无法可靠绑定图片的活动历史 Task MUST 在切换新入口前自然进入终态或由受控运维流程明确收束到 `failed`，否则迁移前检查 MUST 以 `image_processing_history_unresolved` 阻止启用新提交入口，不能被静默忽略。

#### Scenario: 活动父 Job 阻止独立阶段处理
- **WHEN** 同一图片已有 `queued` 或 `running` 的父 Job，用户提交独立阶段处理
- **THEN** 系统返回 `image_processing_active`
- **AND** 不创建独立 Task

#### Scenario: 活动独立 Task 阻止完整重试
- **WHEN** 同一图片已有任意阶段的 `queued` 或 `running` 独立 Task，用户提交完整重试
- **THEN** 系统返回 `image_processing_active`
- **AND** 不创建父 Job revision

#### Scenario: 相同请求并发到达
- **WHEN** 两个请求并发对同一空闲图片发起完整重试或修复
- **THEN** 最多一个请求创建新的父 Job
- **AND** 其他请求返回 `image_processing_active`，不得复用第一个请求创建的活动 Job

#### Scenario: 终态历史不阻止新请求
- **WHEN** 同一图片只有终态父 Job 和终态独立 Task
- **THEN** 合法的完整重试或修复可以创建新的执行身份
- **AND** 既有历史记录保持不变
