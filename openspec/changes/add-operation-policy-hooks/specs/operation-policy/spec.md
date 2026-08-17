## Purpose

为 scope-bound 公共核心提供不依赖用户、订阅或支付实体的操作可用性边界，使开源部署保持 allow-all 行为，同时让适配宿主能够在真实副作用前原子预占、计量或拒绝高成本操作。

## ADDED Requirements

### Requirement: 操作策略必须使用服务端可信上下文

系统 MUST 使用稳定的服务端 operation 标识和当前可信 scope 构造策略请求。首期标识 MUST 包含 `image.upload`、`analysis.agent`、`analysis.reverse_image_search` 和 `image.delete`。客户端提交的 `scope_id`、`user_id`、套餐、余额、operation 结果、grant 或 provider 状态 MUST NOT 覆盖策略请求。非 allow-all 宿主遇到未知 operation、缺失 scope 或无效策略适配器时 MUST fail-closed。

#### Scenario: 客户端伪造 operation 上下文
- **WHEN** 客户端提交另一个 scope、用户、套餐或 grant 标识
- **THEN** 系统忽略或拒绝这些字段，并仅使用服务端解析的 scope 和业务流程确定 operation
- **AND** 不访问或泄露伪造 scope 的资源

#### Scenario: 非 allow-all 宿主缺少策略适配器
- **WHEN** 宿主未安装有效的 operation policy 或 policy 返回不可识别的 operation
- **THEN** 系统拒绝该操作
- **AND** 不回退到 allow-all 或 local scope

### Requirement: 策略必须区分查询、取得执行权和计量终态

系统 MUST 提供非权威的 `probe` 查询以及权威的 `acquire`、`commit`、`release` 生命周期。`probe` MUST NOT 预占或消耗 operation 使用权，真实副作用前 MUST 再次执行 `acquire`。同一幂等键的 acquire、commit 和 release MUST 可安全重复处理；只有能够确定外部副作用尚未发生时才允许 release。策略永久禁止、operation 限制和策略不可用 MUST 分别使用 `operation_forbidden`、`operation_limit_exceeded` 和 `operation_policy_unavailable` 终止被策略控制的 operation，不得继续该 operation 的副作用。父工作流是否失败或降级 MUST 由对应 capability 明确定义；其中 `analysis.reverse_image_search` 在任务策略为 `auto` 且仅因 operation policy 拒绝时，MUST 不调用 provider，但 MUST 返回可降级结果并允许 Agent 继续离线分析。policy 拒绝结果可以包含可选 `retry_at`；核心 MUST NOT 计算限制周期，也 MUST NOT 因到达该时间自动重新执行。

#### Scenario: 查询后额度被并发请求占用
- **WHEN** 两个请求先后 probe 得到可用，但只有一个请求能原子 acquire 最后一个名额
- **THEN** 未取得 grant 的请求在副作用前被拒绝
- **AND** 不因 probe 结果而绕过 acquire

#### Scenario: 策略拒绝上传
- **WHEN** `image.upload` 的 acquire 被拒绝
- **THEN** 系统不写入目标文件、不创建 Meme、不提交后续任务
- **AND** 返回稳定的策略错误

#### Scenario: 外部结果未知
- **WHEN** 进程在 commit 后、外部 provider 结果确认前中断
- **THEN** grant 保持已计量或未知终态，不释放为可用额度
- **AND** 系统不得自动重放同一未知结果的付费 provider 调用

### Requirement: 可用性查询必须可展示但不能替代执行授权

系统 MUST 提供按当前 scope 查询首期 operation 可用性的能力，响应最多包含可用状态、稳定原因和 policy 可选提供的 `retry_at`，不得返回其他 scope、grant、套餐内部字段或供应商凭据。任何执行入口 MUST 将该查询和 `retry_at` 视为提示，并在实际副作用前重新 acquire。

#### Scenario: 开源 allow-all 查询
- **WHEN** 开源入口查询任一已知 operation 的可用性
- **THEN** 系统返回可用
- **AND** 不生成 reservation 或 usage 记录

#### Scenario: 宿主 operation 限制查询
- **WHEN** 宿主 policy 判断某 scope 的 operation 当前不可用并返回 `operation_limit_exceeded` 和可选 `retry_at`
- **THEN** 查询返回不可用、相同稳定原因及 policy 提供的可选提示时间
- **AND** 查询本身不改变 operation 使用权，也不安排自动重试

### Requirement: 图片上传必须在持久化边界使用策略

系统 MUST 在扩展名、大小、文件名和图片内容校验通过后、开始 durable upload 前 acquire `image.upload`。只有实际创建新 Meme 并完成文件和数据库持久化后才 commit；可证明没有发生 durable upload 时 MUST release。普通上传和合集导入的新图片 MUST 使用同一语义，复用已有 Meme 的合集成员不得重复 acquire。

#### Scenario: 合法新图片上传
- **WHEN** 图片通过校验且 `image.upload` acquire 成功
- **THEN** 系统完成当前 scope 的文件和 Meme 持久化后 commit 一次
- **AND** 创建或复用该图片的统一图片处理 job，不为尚未需要的 Agent 阶段提前 acquire

#### Scenario: 合集导入复用已有图片
- **WHEN** 合集包成员命中当前 scope 中已有 Meme
- **THEN** 系统复用该 Meme
- **AND** 不取得或消耗新的 `image.upload` grant

#### Scenario: 批量上传部分耗尽额度
- **WHEN** 批量请求中部分项目 acquire 成功、部分项目被拒绝
- **THEN** 成功项目独立完成并返回结果
- **AND** 被拒绝项目不产生文件、Meme 或后续任务

#### Scenario: 上传失败且 durable 状态未知
- **WHEN** Meme 提交失败，且暂存或目标文件无法确认已经清理或隔离
- **THEN** 系统不得报告上传成功，也不得 release 为确定未使用的额度
- **AND** operation 保持可诊断的 committed/unknown 状态，等待恢复流程收束

### Requirement: Agent 策略必须绑定逻辑分析任务

系统 MUST 由图片处理 Worker 在确认当前图片没有有效 Agent 语境、且活动 `meme_context_generation` Task dedupe 完成之后，为新的逻辑 Agent Task acquire 一次 `analysis.agent`。系统 MUST 使用服务端预生成 task id 或等价稳定 `logical_request_key` 保证并发 acquire 幂等，并把 grant 与持久 Agent Task 可信关联；grant 不得绑定 execution attempt，也不得从客户端 payload 接受或覆盖。同一 Agent Task 的 Worker 自动重试、租约恢复、claim 变化和终态写回 MUST 复用原 grant，不重复计量；用户主动重试终态图片处理 job MUST 创建新 job revision 和新 Agent Task 并重新 acquire。Agent 外部执行开始前 MUST commit；只有尚未开始且确定无副作用的 reservation 才允许 release。每个 attempt MUST 关联该 Task 并持久化准备、grant 已提交、外部调用已开始、完成或未知执行状态；外部调用已开始后结果未知时 MUST 保留计量事实、让 Task 以稳定错误收束并禁止自动重放。

#### Scenario: 有效语境或活动任务去重
- **WHEN** 相同图片内容和 Agent 配置已有有效语境，或已有活动 `meme_context_generation` Task
- **THEN** 系统复用有效产物或 Agent Task
- **AND** 不取得第二个 `analysis.agent` grant

#### Scenario: 并发创建同一逻辑 Agent Task
- **WHEN** 两个 Worker 同时确认同一图片版本和 Agent 配置需要新的 Agent Task
- **THEN** 稳定 logical request key 和活动 Task 唯一约束只产生一个 grant 与一个 Agent Task
- **AND** 竞争失败者复用已有 Task/grant，不建立第二个 reservation

#### Scenario: Agent operation 被限制
- **WHEN** 新逻辑 Agent Task 的 acquire 被拒绝
- **THEN** 系统不启动 OpenCode，并让图片处理 job 的 Agent 阶段进入 `blocked`，保存稳定原因和 policy 可选提供的 `retry_at`
- **AND** 已成功保存的图片和有效视觉产物保持可查看，且只有显式重试或受控恢复才能创建新的逻辑 Agent Task

#### Scenario: Worker 自动重试
- **WHEN** 同一 Agent Task 因可证明安全的暂态错误、租约过期或 Worker 重启而再次执行
- **THEN** 系统继续使用原 Task 和 grant
- **AND** 不重复消耗 `analysis.agent` operation 使用权

#### Scenario: 用户主动重试
- **WHEN** 用户对 Agent 阶段为 `failed`、`blocked` 或 `unknown_execution` 的终态图片处理 job 发起重试
- **THEN** 系统创建新的 job revision 和 Agent Task，并取得新的 `analysis.agent` grant
- **AND** 旧 job、旧 Task 和旧 grant 不被重新激活

#### Scenario: Agent 外部结果未知
- **WHEN** Agent grant 已 commit，但 Worker 无法确认绑定 Task/attempt 的外部结果
- **THEN** Agent Task 以 `failed` 终态和 `unknown_execution` 错误码收束，父 job 阶段进入 `unknown_execution`，grant 保持已计量或未知终态
- **AND** 系统不自动重放 OpenCode、联网检索或其它付费副作用

#### Scenario: grant 已提交但外部调用尚未开始
- **WHEN** Worker 在 commit 成功后、持久化 `external_started` 之前退出
- **THEN** 恢复者使用同一 Agent Task、attempt 和 grant 开始首次外部调用，不重新 acquire 或 commit
- **AND** 一旦无法证明调用尚未开始，阶段转入 `unknown_execution` 而不是自动执行

#### Scenario: 文本 embedding 阶段执行
- **WHEN** 图片处理 job 在 Agent 语境有效后执行单图文本 embedding
- **THEN** 系统不复用或消耗 `analysis.agent` grant
- **AND** 如需单独计量，必须使用未来独立定义的 operation

### Requirement: 联网反向图片检索必须只在真实 provider 调用时计量

系统 MUST 将 `analysis.reverse_image_search` 限定为联网反向图片 provider 的一次逻辑检索。有效缓存命中、本地视觉相似搜索和普通网页搜索不得使用该 operation。缓存键的互斥范围内 MUST 二次检查缓存，并在确认 miss 且准备联系 provider 前 acquire；provider 调用开始时 MUST commit 同一逻辑 request，重复 request_id 不得重复计量。

#### Scenario: 反向图片缓存命中
- **WHEN** Agent 请求反向图片检索且缓存锁内发现有效缓存
- **THEN** 系统返回缓存结果
- **AND** 不取得或消耗 `analysis.reverse_image_search` grant，不联系 provider

#### Scenario: 反向图片 operation 受限且策略为 auto
- **WHEN** 缓存未命中但 `analysis.reverse_image_search` acquire 被拒绝，且任务策略为 `auto`
- **THEN** 内部检索接口返回稳定的不可用原因和可降级标记
- **AND** Agent 继续离线分析，不改为普通网页搜索

#### Scenario: 反向图片策略为 forbid
- **WHEN** 任务策略禁止反向图片检索
- **THEN** 内部接口拒绝该检索
- **AND** 不读取缓存、不取得 grant、不联系 provider

#### Scenario: 反向图片 provider 明确失败
- **WHEN** provider 明确返回失败、空结果或完整但非法的响应
- **THEN** 本次逻辑 request 保留已计量事实和稳定 provider 错误
- **AND** 同一 request_id 的恢复不再次调用 provider 或重复计量

#### Scenario: 反向图片调用开始后进程中断
- **WHEN** usage 已记录 `provider_started`，但进程在 commit 或响应写回前中断
- **THEN** 恢复流程保留该 request 的 committed/unknown 事实并只查询可验证的既有结果；无法验证结果时返回稳定 `reverse_image_unknown_execution` 和可降级标记
- **AND** 不 release grant、不生成新 request_id，也不再次联系 provider；`auto` Agent 可以继续离线分析，不能把该子调用未知状态冒充为整个 Agent Task 的 `unknown_execution`

### Requirement: 删除必须在不可逆副作用前可拒绝

系统 MUST 在删除数据库记录或文件之前 acquire `image.delete`，并在删除完成后提交该 operation。策略拒绝或策略不可用时 MUST 不删除文件、Meme、向量、任务或合集关联；删除成功不得返还已经提交的上传或分析额度。

#### Scenario: 删除被策略拒绝
- **WHEN** `image.delete` acquire 被拒绝
- **THEN** 系统返回稳定拒绝错误
- **AND** 图片及其关联数据保持不变

#### Scenario: 删除成功
- **WHEN** 删除通过 policy 且所有不可逆副作用完成
- **THEN** 系统提交 `image.delete`
- **AND** 不增加任何上传或分析可用额度

#### Scenario: 删除发生部分副作用
- **WHEN** 文件或数据库中的一部分删除已经发生，而其余步骤失败或状态无法确认
- **THEN** 系统不得把 grant release 为未使用，也不得自动重放整个删除
- **AND** operation 保持 committed/unknown，并由恢复流程幂等收束剩余状态

### Requirement: 开源默认策略必须显式 allow-all 且保持兼容

系统 MUST 提供显式安装的 `AllowAllOperationPolicy`，使开源 local 入口对所有已知 operation 的 probe 和 acquire 返回允许，并正确完成 commit/release。开源核心 MUST NOT 实现账户、认证、订阅、套餐、额度数值、支付或 provider 密钥管理。

#### Scenario: 开源入口保持原有上传和分析
- **WHEN** 开源 local 应用使用 `AllowAllOperationPolicy` 处理合法上传、Agent 分析或联网反向图片请求
- **THEN** 既有 API、任务、缓存和结果行为保持不变
- **AND** 不产生 policy 导致的 `blocked` 或 `operation_limit_exceeded`，也不需要客户端提供用户、scope、套餐或 grant 字段
