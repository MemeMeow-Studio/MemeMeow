## Purpose

为 Agent 到核心 API 的内部 callback 提供与用户鉴权和商业策略解耦的服务间信任边界，确保每次调用同时绑定受信服务、当前持久 Task 执行权、scope 和目标图片版本，并在旧 claim、伪造任务或重放请求产生副作用前拒绝。

## ADDED Requirements

### Requirement: Agent callback 必须先认证服务再解析业务请求

所有 Agent 内部 callback MUST 在读取或解析请求体、查询 `task_id`、恢复 scope 或访问业务存储之前验证服务间凭据。callback 启用但验证能力缺失、配置为空或验证异常时 MUST fail-closed。Compose 内网、回环地址、来源 IP、难猜的 `task_id` 或用户登录态 MUST NOT 单独构成服务间认证。

#### Scenario: 未认证请求携带有效任务 ID
- **WHEN** 调用方不携带有效服务间凭据，但请求中包含一个真实且正在运行的 `task_id`
- **THEN** 系统在读取业务请求体和查询 Task 前返回稳定的未认证错误
- **AND** 不泄露该 Task 是否存在、所属 scope、类型或状态

#### Scenario: callback verifier 未装配
- **WHEN** 应用启用了 Agent callback 路由但没有装配有效的服务间验证能力
- **THEN** callback 保持不可用并拒绝全部调用
- **AND** 系统不回退到无认证、local scope 或用户认证

#### Scenario: 超限 multipart 未认证请求
- **WHEN** 未认证调用方发送超限或恶意 multipart 请求
- **THEN** 系统在读取图片正文前拒绝请求
- **AND** 不查询 Task、不写临时文件，也不调用 provider

### Requirement: callback 必须证明当前 Task 执行权

通过服务认证后，callback MUST 要求受服务端管理的执行绑定，并验证其 `task_id`、`Task.scope_id`、claim generation、lease owner、允许的 callback operation、目标 SHA 和有效期与当前持久 Task 一致。Task MUST 为允许该 callback 的类型、处于 `running`、具有非零 claim generation 和未过期租约；仅知道 `task_id`、仅持有服务级凭据或提交未受保护的 claim 字段 MUST NOT 获得执行权。任务重新认领、租约过期、取消或进入终态后，旧执行绑定 MUST 立即失效。

#### Scenario: 旧 claim 在重新认领后回调
- **WHEN** 旧 Worker 或 Agent 使用上一代 claim 的执行绑定调用已经被新 Worker 重新认领的 Task
- **THEN** 系统拒绝该 callback
- **AND** 不产生进度、缓存、usage、provider 调用、结果写回或后续阶段推进

#### Scenario: 运行任务缺少有效 claim
- **WHEN** Task 状态为 `running`，但 claim generation 为零、lease owner 缺失或租约已过期
- **THEN** 系统拒绝 callback
- **AND** 不把 `running` 状态本身视为执行授权

#### Scenario: 服务凭据被用于另一个任务
- **WHEN** 已认证服务把某次执行的 callback 绑定与另一个 `task_id`、operation 或目标图片组合使用
- **THEN** 系统以稳定的执行无效错误拒绝请求
- **AND** 不泄露另一个 Task 是否存在或属于哪个 scope

### Requirement: scope 和目标必须从可信任务事实恢复

callback MUST 从持久 Task 及其当前执行绑定恢复 scope、Meme、目标 SHA、attempt 和允许的业务操作，并 MUST 验证构造出的 scope 服务及其相关子服务都绑定同一 scope。请求体、查询参数、Header 或 Agent 结果中的 `scope_id`、`user_id`、Meme、绝对路径、图片 SHA、claim、grant、attempt 或 operation MUST NOT 覆盖可信事实。scope 装配不一致、目标图片变化或 Task 与目标不匹配时 MUST fail-closed，不得回退到 local。

#### Scenario: factory 返回错误 scope 的服务
- **WHEN** callback 从 Task 恢复 scope A，但服务工厂返回 scope B 的任一业务服务
- **THEN** 系统在业务调用前拒绝整个 callback
- **AND** 不终止其它 Worker 的有效 claim，也不访问 scope A 或 B 的业务数据

#### Scenario: 视觉匹配目标 SHA 不一致
- **WHEN** 视觉匹配 callback 对应 Task 的目标 SHA 与当前 Meme 或视觉 embedding 的图片 SHA 不一致
- **THEN** 系统拒绝匹配并要求图片处理流程重新收束
- **AND** 不返回候选图片或使用过期向量

#### Scenario: 反向图片请求替换目标
- **WHEN** 反向图片 callback 上传的图片既不等于 Task 目标 SHA，也不是由后端从该目标生成并绑定到当前执行的受控派生图
- **THEN** 系统拒绝请求
- **AND** 不读取缓存、不记录 usage、不联系 provider

### Requirement: callback 重放和副作用必须受当前执行与幂等事实约束

可能产生 provider 调用、usage、缓存写入、业务写入、进度或终态变化的 callback MUST 使用服务端校验的请求标识，并将其与 Task、当前 claim、operation、attempt 和目标输入绑定。相同绑定的重复请求 MUST 返回或恢复同一既有事实，不得重复副作用；绑定冲突、旧 claim 重放或无法确认外部结果的请求 MUST 被拒绝或以既有 `unknown_execution` 协议收束，不得换新请求标识自动重放。

#### Scenario: 同一请求重复提交
- **WHEN** 当前执行因网络重试重复提交相同 request id 和相同输入摘要
- **THEN** 系统复用同一 callback 结果或持久副作用事实
- **AND** 不重复调用 provider、计量、写缓存或推进任务

#### Scenario: request id 被改绑
- **WHEN** 调用方把已使用的 request id 与不同 Task、claim generation、operation、目标 SHA 或输入摘要组合
- **THEN** 系统拒绝该请求并记录受限安全诊断
- **AND** 不执行新的业务副作用

### Requirement: callback 凭据必须与其它信任域分离

Agent 到 API 的 callback 凭据 MUST 与用户 session、operation policy grant、供应商密钥、数据库凭据及 API 到 executor 的服务凭据分离。Runner 或 executor MUST 只向一次受控 Agent 执行提供其所需的最小 callback 地址、执行绑定和允许操作；凭据 MUST NOT 写入任务普通 payload、结果 artifact、缓存、usage、错误正文或日志。开源入口 MUST 显式配置安全默认实现；宿主使用 token、HMAC 或 mTLS 等替代验证方式时 MUST 保留或加强当前 Task 执行权校验。

#### Scenario: 检查 Agent 执行环境
- **WHEN** Runner 为一个 Agent Task 构造执行环境
- **THEN** 该执行只能获得绑定当前 Task/claim 和允许 callback operation 的凭据
- **AND** 不获得用户 token、operation grant、供应商密钥、数据库凭据或 API 到 executor 的 Bearer token

#### Scenario: 记录 callback 失败
- **WHEN** callback 凭据无效、过期或与当前执行不匹配
- **THEN** 日志只记录脱敏的稳定错误、服务身份摘要和服务端关联标识
- **AND** 不记录原始凭据、用户信息、其它 scope、图片正文或 provider 密钥

### Requirement: callback 拒绝必须稳定且不产生存在性泄漏

认证失败 MUST 返回统一的 `agent_callback_unauthorized`；服务已认证但执行绑定、Task、claim、scope、operation 或目标校验失败时 MUST 返回统一的 `agent_callback_invalid_execution`。外部响应 MUST NOT 区分 Task 不存在、跨 scope、类型错误、旧 claim、租约过期或目标变化。所有拒绝 MUST 发生在对应业务副作用之前，并允许服务端日志保留不返回给调用方的受限诊断。

#### Scenario: 探测多个任务 ID
- **WHEN** 调用方使用同一无效或过期凭据探测不存在、其它 scope 和当前 scope 的任务 ID
- **THEN** 系统返回不可区分的稳定错误
- **AND** 响应大小和公开字段不暴露任务归属或状态

#### Scenario: 验证失败不产生副作用
- **WHEN** 任一 callback 在服务认证、当前 claim、scope、operation 或目标校验中失败
- **THEN** 系统不创建或更新 usage、缓存、Meme、向量、Task 结果或图片处理阶段
- **AND** 不调用视觉、Agent、embedding 或联网 provider
