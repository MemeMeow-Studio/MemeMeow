## Purpose

为使用 OpenCode 的 Agent 任务提供按模型等级区分的分析金额策略，在接近边界时提醒 Agent 收尾，并在达到更高边界后由 Executor 停止运行，同时保留可诊断且不向普通用户直接暴露金额阈值的终态。

## ADDED Requirements

### Requirement: 公共能力默认关闭并冻结启用状态

公共核心 MUST 提供冻结金额策略、提醒插件、Executor 金额终止和持久化，默认 MUST 关闭提醒和金额终止。Server MUST 保留三等级目录并显式启用。策略必填、插件门禁、金额读取和金额终止要求仅适用于冻结为启用的任务；模型等级目录要求由 Server 配置层承担。

启用状态 MUST 由可信配置决定并随新任务冻结，客户端不得覆盖。恢复 MUST 沿用冻结状态，后续关闭部署配置不得绕过已启用任务的原策略。历史无策略任务和创建时未启用的任务 MUST 沿用原行为。

#### Scenario: 公共默认配置未启用
- **WHEN** 使用公共默认配置创建任务
- **THEN** 不要求金额策略、不加载插件、不执行插件启动门禁或金额终止检查，现有超时和取消保持正常

#### Scenario: Server 显式启用
- **WHEN** Server 按三等级目录创建新任务
- **THEN** 冻结启用状态及对应金额策略，执行插件门禁、提醒和独立金额终止

#### Scenario: 部署关闭后恢复已启用任务
- **WHEN** 已启用任务恢复时部署配置已经关闭该能力
- **THEN** 仍沿用冻结策略和累计金额，继续执行原提醒及终止规则

### Requirement: 每个模型等级必须有可冻结的分析用量策略

系统 MUST 为受控模型目录中的 `model_free`、`model_standard`、`model_plus` 等模型等级键提供分析用量策略。策略 MUST 显式包含提醒金额限额和终止金额限额，两个值 MUST 为有效正数且终止金额限额 MUST 大于提醒金额限额。配置工具在没有单独指定终止金额限额时 MUST 使用提醒金额限额的两倍，任务保存的策略 MUST 包含两个明确数值。策略 MUST 由服务端按照受控模型目录键选择，客户端和任务 payload 不得提交或提高金额限额。任务创建或恢复时 MUST 保存模型目录键、实际模型、变体和完整策略快照，Executor MUST 拒绝与受信任快照不一致的输入；之后目录配置变化不得改写该任务的策略。

#### Scenario: 使用初始模型等级策略
- **WHEN** 系统加载受控模型目录
- **THEN** `model_free` 使用提醒 0.05 美元和终止 0.10 美元，`model_standard` 使用提醒 0.10 美元和终止 0.20 美元，`model_plus` 使用提醒 0.15 美元和终止 0.30 美元

#### Scenario: 使用两倍终止限额默认值
- **WHEN** 新模型等级只配置提醒金额限额为 0.10 美元
- **THEN** 配置工具生成提醒金额限额 0.10 美元、终止金额限额 0.20 美元的完整策略，任务创建时保存这两个明确数值

#### Scenario: 使用明确的非两倍终止限额
- **WHEN** 某模型等级明确配置提醒金额限额 0.40 美元和终止金额限额 1.00 美元
- **THEN** 系统保存并使用 0.40 美元和 1.00 美元，不改写金额关系

#### Scenario: 策略配置无效
- **WHEN** 模型目录键缺少分析用量策略、限额不是有效正数或终止金额限额不大于提醒金额限额
- **THEN** 系统拒绝创建任务并返回稳定的配置错误，不以无限分析程度继续执行

#### Scenario: 客户端尝试提高限额
- **WHEN** 客户端或任务 payload 提交与服务端模型目录策略不同的提醒金额或终止金额
- **THEN** 系统拒绝该值，任务和 Executor 只使用服务端冻结的受信任策略快照

### Requirement: 插件必须在达到提醒限额后尽力发送一次收尾提醒

系统 MUST 使用 OpenCode 现有插件接口读取当前 attempt 主 session 已完成请求的累计金额。插件 MUST 只统计冻结的主 session，不递归汇总子 Agent 或子 session，也不得把其他 session 的金额混入当前任务。当累计金额达到或超过冻结策略的提醒金额限额时，插件 MUST 在该 attempt 内最多一次向后续模型请求追加收尾系统提示，要求 Agent 尽快完成必要工作、验证已有结果并生成报告。恢复产生的新 attempt 在达到阈值且提醒接口正常时 MUST 发送本 attempt 的一次提醒；沿用同一 session 的累计金额，不继承旧 attempt 的已提醒标记。该提示 MUST 只影响模型上下文，不得创建新的用户任务。插件读取失败、提醒失败或模型不遵从提醒时 MUST 记录明确错误，不得改变 Executor 对终止限额的独立判断。

#### Scenario: 达到提醒限额
- **WHEN** 某 attempt 的提醒金额限额为 0.10 美元，插件在下一次模型请求前确认主 session 已完成用量为 0.11 美元且尚未提醒
- **THEN** 当前请求包含一次收尾系统提示，并记录该 attempt 已提醒

#### Scenario: 提醒不重复发送
- **WHEN** 已提醒的 attempt 因模型重试、上下文压缩或后续请求再次满足提醒条件
- **THEN** 插件不重复追加收尾提示，也不额外创建用户消息

#### Scenario: 子 session 不计入累计用量
- **WHEN** OpenCode 历史中存在不属于冻结主 session 的子 session 或其他 session
- **THEN** 插件和 Executor 都不把这些 session 的金额加入当前任务累计金额

#### Scenario: 插件无法读取用量
- **WHEN** 已成功加载的插件在运行期间无法取得当前主 session、累计金额或完整历史
- **THEN** 系统记录可诊断的提醒读取错误，允许任务继续，并由 Executor 独立执行终止判断

### Requirement: Executor 必须在达到终止限额后停止 Agent

生产 Executor MUST 使用绑定当前 executor attempt 的短期模型 capability，从部署固定的模型 broker `/analysis-usage` 接口读取累计金额，作为终止判断的唯一金额事实。broker MUST 校验 capability 与请求头中的 executor attempt 标识一致，并返回该 attempt 已完成模型调用的累计金额、检查时间和同一 attempt 标识；恢复 attempt 的累计金额 MUST 以持久化的最近可信金额为起点。Executor MUST 拒绝跳转、attempt 不匹配、金额倒退、无时区检查时间、超大响应、无效金额和读取故障。任务专属 `OPENCODE_DB` 只供 OpenCode 和提醒插件使用，不能决定生产终止。插件 MUST 通过同一 OpenCode 进程的 session SDK 投影读取主 session 累计金额，不得自行重新计算 token 价格。当 broker 观测金额达到或超过冻结策略的终止金额限额时，Executor MUST 阻止任务继续正常运行，直接复用进程组 `SIGTERM`、有限等待、必要时 `SIGKILL` 和 `wait` 回收流程，不新增 OpenCode 协作式中断通道。系统不要求在单次模型请求进行中预测最终金额，也不保证最终金额严格小于或等于终止金额限额；Executor 下一次检查前已经完成的一个或多个调用所产生的超过属于允许行为。

#### Scenario: 完成一次调用后超过终止限额
- **WHEN** 终止金额限额为 0.20 美元，调用前累计金额为 0.19 美元，本次调用完成后累计金额为 0.23 美元
- **THEN** Executor 在观测到 0.23 美元后终止任务，不再允许任务正常继续，并保留最终观测值供内部诊断

#### Scenario: 插件提醒失败不影响终止
- **WHEN** 插件未能发送收尾提醒，但 Executor 观测到累计金额已经达到终止金额限额
- **THEN** Executor 仍独立终止任务，插件失败不会使任务获得无限分析程度

#### Scenario: Agent 修改任务专属 OpenCode 数据库
- **WHEN** Agent 修改本任务 SQLite 中的 session 金额或提醒状态
- **THEN** 模型 broker 保存的 attempt 累计金额保持不变，Executor 继续根据 broker 金额执行终止

#### Scenario: broker 用量协议无法确认
- **WHEN** broker 请求跳转、attempt 不匹配、金额倒退、响应无效或查询失败
- **THEN** Executor 使用 `agent_analysis_usage_unavailable` 触发进程回收，不继续无限分析

#### Scenario: SIGTERM 后进程没有及时退出
- **WHEN** Executor 向进程组发送 SIGTERM 后，进程没有在现有有限等待时间内退出
- **THEN** Executor 发送 SIGKILL 并通过 wait 确认回收，插件与 OpenCode 协作式中断接口不参与进程回收

#### Scenario: 无法确认进程回收
- **WHEN** 金额超限、插件启动失败或用量读取失败触发终止，但 Executor 无法确认进程已回收
- **THEN** 系统使用 `unknown_execution`，受保护诊断保留原始触发原因、失败阶段及回收结果

### Requirement: 分析程度终止必须使用稳定且不直接暴露金额的公开原因

因终止金额限额停止且确认进程已回收的任务 MUST 进入失败终态。面向用户、公开 API 和任务状态的稳定错误码 MUST 为 `agent_maximum_analysis_depth_exceeded`，显示消息 MUST 为“超过最大分析程度”，不得直接表述为金额、费用或计费超限。内部日志和受保护诊断 MUST 保留经过脱敏的模型等级、提醒金额限额、终止金额限额、最终观测金额、检查阶段、终止信号和进程回收结果。无法确认回收时 MUST 使用 `unknown_execution`，不能用金额终止原因掩盖执行状态不明。

#### Scenario: 用户查看分析程度终止的任务
- **WHEN** 任务因达到终止金额限额而停止
- **THEN** 用户看到 `agent_maximum_analysis_depth_exceeded` 和“超过最大分析程度”，不看到内部金额阈值

#### Scenario: 运维调查终止原因
- **WHEN** 合法运维人员调查该任务的终止记录
- **THEN** 受保护日志包含最终观测金额、冻结策略和终止阶段，且不包含凭据、提示词、推理正文或其他用户的受保护标识

### Requirement: 恢复任务必须沿用原分析用量策略和累计用量

系统 MUST 以 attempt 记录作为冻结策略、当前 session 标识、最近可信累计金额和该 attempt 提醒状态的权威来源；Task 只保存公开终态和最近可展示摘要。恢复同一 OpenCode session 或重新启动执行进程时 MUST 继续使用原策略，并把最近可信累计金额作为新 broker attempt 的起点，不得因为进程、Worker 或服务重启而重新获得完整分析程度。恢复产生的新 attempt 可以拥有自己的最多一次提醒状态。旧 attempt 的迟到状态 MUST NOT 覆盖新 attempt 的策略、用量或终态。

#### Scenario: 恢复同一 session
- **WHEN** 一个提醒金额限额为 0.10 美元、终止金额限额为 0.20 美元的任务在累计 0.12 美元后恢复同一 session 并创建新 attempt
- **THEN** 新 attempt 继续以 0.12 美元为已有用量，仍使用原来的 0.10 美元和 0.20 美元策略，并可以发送该新 attempt 自己的一次收尾提醒

#### Scenario: 过期 attempt 不能写回
- **WHEN** 旧执行器租约失效且新执行器已经接管任务，旧进程随后报告较低的累计金额或不同提醒状态
- **THEN** 系统拒绝旧 attempt 写回，并保留新执行器确认的事实

### Requirement: 轮次只能作为活动观察信息

系统 MAY 继续统计和展示 Agent 已完成轮次、当前轮次状态及最近活动时间，但 MUST NOT 使用轮次决定分析用量提醒、终止边界或剩余金额。轮次统计不可用时 MUST NOT 绕过基于累计金额的 Executor 终止判断。

#### Scenario: 轮次较多但金额未达到终止限额
- **WHEN** Agent 已执行较多轮次但累计金额仍低于终止金额限额
- **THEN** 系统不因轮次数量终止任务，页面可以继续展示轮次作为活动信息

#### Scenario: 轮次统计不可用但金额达到终止限额
- **WHEN** 页面无法取得轮次快照，而 Executor 已确认累计金额达到终止金额限额
- **THEN** Executor 仍终止任务，轮次观察失败不改变分析程度边界

### Requirement: 提醒插件必须可验证且不得成为终止边界

任务进程启动后，Executor MUST 在有限期限内轮询与当前 attempt 绑定的插件就绪状态，确认与 OpenCode `1.18.18` 兼容的提醒插件已从受保护位置成功加载并完成初始化。找不到插件、加载失败、初始化失败、版本不兼容或就绪超时时，系统 MUST 记录明确原因并终止进程组；允许发现失败之前已经发出模型请求，不要求首次请求前拦截。旧 attempt 的就绪状态、文件存在或没有错误日志均不得替代就绪确认。进程提前结束但未确认就绪时不得记为成功。就绪等待 MUST NOT 阻塞取消、总超时和已可执行的金额检查。插件成功启动后，单次用量读取或提示注入异常 MUST 被明确记录，但任务可以继续；插件 MUST NOT 负责最终终止、任务主状态、计费结算、权限隔离或进程回收。

#### Scenario: 插件加载失败
- **WHEN** 任务启动时找不到插件、插件加载失败、插件版本不兼容或插件初始化失败
- **THEN** Executor 记录具体加载阶段并终止进程组；确认回收后任务进入 `failed`，返回稳定错误 `agent_analysis_reminder_plugin_unavailable`；无法确认回收时使用 `unknown_execution`；允许检测前已有模型请求

#### Scenario: 插件没有报告就绪
- **WHEN** 就绪期限届满仍未收到当前 attempt 的成功初始化状态，或进程提前退出且没有确认就绪
- **THEN** 任务不得进入成功终态；系统记录就绪超时或提前退出的具体原因，并在需要时终止和回收进程

#### Scenario: 插件运行期异常
- **WHEN** 已成功加载的插件在读取用量或追加提醒时异常
- **THEN** 系统记录本次提醒失败并允许任务继续，Executor 仍按冻结策略执行终止判断
