## Purpose

为 Agent 外部执行提供可诊断、可审计且副作用安全的会话续跑能力，使暂态模型网关故障不会丢失已经完成的会话上下文，也不会通过重复任务标识或盲目重放造成额外执行。

## ADDED Requirements

### Requirement: Agent 会话标识必须在失败后可恢复

系统 MUST 为每个 OpenCode Agent 执行记录其明确的 session 标识。即使 OpenCode 以非零状态退出，只要输出或运行时数据库能够证明 session 标识，系统 MUST 保存该标识及其所属任务、scope、输入摘要和最近 attempt；无法证明时 MUST 明确标记为不可续跑，而不得猜测 session。

#### Scenario: OpenCode 在模型网关错误后退出
- **WHEN** Agent 进程在已创建 session 后以非零状态退出
- **THEN** 任务详情保留该 session 标识、首次外部错误和最近 attempt 信息
- **AND** 系统不因进程失败而把 session 标识清空

#### Scenario: 进程未创建可证明的 session
- **WHEN** Agent 在启动前、参数校验或运行时初始化阶段失败
- **THEN** 任务详情不提供虚构的 session 标识，并将任务标记为不可续跑

### Requirement: 续跑必须使用明确的 session 标识

系统 MUST 仅允许通过服务端持久化且与当前任务、scope、图片 SHA-256、处理配置和 attempt 绑定的 session 标识续跑 Agent。系统 MUST NOT 使用未指定目标的全局“继续最近会话”语义。

#### Scenario: 用户或受控恢复器续跑 Agent
- **WHEN** 任务存在可续跑 session 且失败属于允许恢复的暂态错误
- **THEN** 系统使用该 session 标识启动续跑，并向 Agent 发送继续完成未完成工作的指令
- **AND** 续跑仍使用当前任务的受控图片、skill 和配置

#### Scenario: session 与任务事实不匹配
- **WHEN** 请求提供的 session 不属于当前 scope、任务、图片指纹或处理配置
- **THEN** 系统拒绝续跑并返回稳定的 session 绑定错误
- **AND** 不启动 OpenCode 进程

### Requirement: 续跑必须隔离执行尝试并保护副作用

系统 MUST 为每次续跑分配独立的执行尝试标识，且不得把已存在的终态 executor 任务标识再次提交。续跑前 MUST 保留已有 draft 和可验证中间产物；结果写回 MUST 继续验证当前 claim、attempt、session、目标输入和结果文件。

#### Scenario: 终态 executor 任务进入续跑
- **WHEN** 任务需要使用已有 session 再次启动 OpenCode
- **THEN** executor 接受新的 attempt 标识或等价的受控恢复操作，而不是返回 `task_exists`
- **AND** 旧 attempt 的终态和诊断保持可查询

#### Scenario: 续跑期间旧结果晚到
- **WHEN** 旧 attempt 的进度、结果或回调在新 attempt 启动后到达
- **THEN** 系统拒绝旧 attempt 的写回，不覆盖新 attempt 的状态或业务产物

### Requirement: 续跑资格必须按错误和副作用边界判定

系统 MUST 只对可证明为暂态且尚未产生不可恢复外部副作用的错误提供自动或受控续跑。结果文件损坏、输入指纹变化、权限或计量状态不明、外部调用是否发生无法确认等情况 MUST 收束为不可自动续跑，并要求用户显式创建新的处理 revision。

#### Scenario: 模型网关暂时不可用
- **WHEN** Agent 因 429、5xx、连接中断或等价的可恢复网关错误退出，且 session 和 attempt 事实完整
- **THEN** 系统允许按退避策略续跑原 session

#### Scenario: 外部执行结果无法确认
- **WHEN** Worker 在外部执行窗口后崩溃，无法证明请求是否已生效或结果是否已写入
- **THEN** 任务进入不可自动续跑的 `unknown_execution` 诊断
- **AND** 系统不得自动重放该外部调用

### Requirement: 续跑历史必须保留首次错误

系统 MUST 将首次外部失败、每次续跑 attempt 和最终收束错误作为有序历史保存。后续调度或 executor 错误 MUST NOT 覆盖首次错误；任务详情默认展示最终状态，同时提供有限且脱敏的首次失败摘要和 attempt 次数。

#### Scenario: 续跑后仍然失败
- **WHEN** 原 session 续跑达到上限后再次失败
- **THEN** 任务同时保留首次模型网关错误、续跑次数和最终失败错误
- **AND** API 不返回 prompt、工具参数、密钥或完整 transcript
