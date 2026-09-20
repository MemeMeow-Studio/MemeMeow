## Context

生产 Agent 镜像固定使用 OpenCode `1.18.18`。OpenCode 在任务专属 SQLite 中保存 session 累计 `cost`，插件通过当前 OpenCode 进程提供的 SDK 读取主 session 已完成请求的用量。Executor 从同一数据库读取绑定主 session 的累计金额，负责检查取消和总超时，并能终止及回收整个进程组。

提醒和终止共同使用 OpenCode 记录的主 session 累计金额。现有轮次字段继续服务页面活动观察，不进入提醒或终止判断。

## Goals / Non-Goals

**Goals:**

- 每个模型等级具有独立、可冻结的提醒金额限额和终止金额限额。
- 插件在达到提醒金额后为每个 attempt 最多一次提示 Agent 收尾。
- Executor 在达到终止金额后停止 OpenCode，并把公开原因表达为“超过最大分析程度”。
- 恢复同一 session 时继续使用原策略和累计金额。
- Task 与 attempt 在同一 claim fencing 事务中保存最终摘要和终止诊断。
- 公共默认配置保持现有执行行为，Server 通过可信模型目录显式启用。

**Non-Goals:**

- 不保证最终金额严格小于或等于终止限额。
- 不按流式 token 预测费用，不在 provider 请求执行期间进行金额截断。
- 不让插件负责最终终止、任务主状态、进程回收或供应商账单结算。
- 不使用轮次数量决定提醒或终止。
- 不在本 change 中增加订阅扣费或账户余额结算。
- 不递归汇总子 Agent 或子 session 的金额。

## Decisions

### 1. 公共默认关闭，启用状态随任务冻结

公共核心提供策略解析、插件、Executor 金额终止、响应字段和 PostgreSQL 持久化。公共默认配置不生成策略，因此不加载插件，也不执行插件门禁或金额检查。超时和取消继续使用现有流程。

Server 模型目录显式启用能力并生成完整策略。启用状态随任务创建冻结，客户端不能覆盖。历史无策略任务和创建时未启用的任务维持原行为；已经启用的任务在恢复时继续执行原策略，即使部署配置后来关闭该能力。

### 2. 冻结策略保存两个明确金额限额

策略包含版本、模型目录键、实际模型、变体、币种、`reminder_cost` 和 `termination_cost`。两个限额必须为有限正数，`termination_cost` 必须大于 `reminder_cost`。配置工具在没有提供终止限额时使用提醒限额的两倍，并把计算结果写入冻结策略。

Server 初始策略如下：

- `model_free`: 提醒 0.05 美元，终止 0.10 美元。
- `model_standard`: 提醒 0.10 美元，终止 0.20 美元。
- `model_plus`: 提醒 0.15 美元，终止 0.30 美元。

服务端根据实际模型目录键选择策略。任务 payload 不能提供金额限额，Executor 只接受已经通过公共解析器校验的策略快照。任务开始后的目录变化只影响之后创建的任务。

### 3. 插件读取主 session 用量并发送一次提醒

插件使用 `experimental.chat.system.transform`，从当前请求取得 `sessionID`，通过 OpenCode SDK 读取冻结主 session 已完成请求的累计金额。插件不自行计算 token 价格，也不汇总子 session 或其他 session。

累计金额达到 `reminder_cost` 后，插件向系统提示追加固定中文收尾要求，并通过 Executor 创建的 Unix socket 报告当前 attempt 已提醒。请求重试、上下文压缩和后续调用不会重复提醒。恢复产生的新 attempt 拥有独立提醒状态，并继续读取同一 session 的累计金额。

插件状态绑定 attempt 标识、策略版本、插件版本和 session。运行期间无法读取用量或追加提示时，插件记录明确错误并允许任务继续；Executor 的终止判断不读取插件提醒结果。

### 4. Executor 从主 session 读取累计金额并终止进程组

Executor 使用传给 OpenCode 的 `OPENCODE_DB` 定位任务数据库，在进程运行期间从当前进程输出取得并冻结主 session ID，按该 ID 读取 `session.cost`。查询核验主 session 的 `parent_id` 和工作目录，不选择最新 session，不汇总子 session。首次启动允许有界等待绑定；期限届满仍未绑定，或进程退出时缺少绑定，返回 `agent_analysis_usage_unavailable`。

SQLite 连接以只读模式打开。数据库缺失、schema 不兼容、session 或目录不匹配、金额无效、金额减少和读取异常均产生 `agent_analysis_usage_unavailable`，保留明确原因并触发进程回收。模型调用继续使用现有凭据和请求路径。

检查在现有进程轮询中执行。金额达到或超过 `termination_cost` 时，Executor 调用现有 `ProcessSupervisor`：向进程组发送 `SIGTERM`，等待有限时间，必要时发送 `SIGKILL`，最后通过 `wait` 确认回收。进程回收无法确认时，任务使用 `unknown_execution`。

OpenCode 在请求完成后更新累计金额，检查也存在固定间隔。因此一次或多个已经完成的调用可能让最终金额超过终止限额。该限额表示分析程度边界，不承担财务结算。

### 5. 启用策略时强制任务级系统隔离

启用策略的任务必须经过 bubblewrap。挂载视图先隐藏 runtime、workspace 和图片根目录，再挂入当前任务 scratch、结果目录、输入图片、Skill、插件及可选候选目录。任务使用独立 `OPENCODE_DB`，OpenCode 的 `--dir` 和进程工作目录均指向当前 scratch。

bubblewrap 使用独立 PID namespace 和 procfs，并以空文件遮蔽 Executor token。Agent 不能读取宿主 Executor 环境、其他任务目录或其他图片。模型 capability 仍进入当前 OpenCode 环境，用于调用 broker；Server 必须把该 capability 绑定当前 attempt、模型及有效期。

### 6. 插件就绪门禁绑定当前 attempt

启用策略的任务启动后，Executor 在有限期限内读取 Unix socket 状态，确认插件版本、策略版本与 attempt 标识匹配，并确认插件已经完成初始化。旧 attempt 状态或没有错误日志不能表示当前插件就绪。

插件缺失、加载失败、初始化失败、版本不兼容、状态损坏或就绪超时会触发进程组终止。OpenCode `1.18.18` 可能在插件失败时继续执行，因此允许检测完成前已经发出模型请求。未确认就绪的进程即使提前退出，也不能进入成功终态。

插件与 SDK 的接口按照 OpenCode `1.18.18` 验证。共享依赖预装在 Agent 镜像的受保护路径，任务不安装独立依赖副本。

### 7. 公开错误与受保护诊断分开保存

金额达到限额并确认进程回收后，公开稳定错误码为 `agent_maximum_analysis_depth_exceeded`，显示消息为“超过最大分析程度”。公开 Task DTO 不返回金额阈值、最终观测金额、提示词、推理正文或凭据。

Executor 错误只携带固定诊断字段：阈值、最终观测金额、检查阶段、原始触发原因、终止信号和回收结果。`AgentExecutorClient` 丢弃嵌套诊断中的未知字段，只把受控摘要传给后端。受保护日志记录相同类别，并执行既有脱敏处理。

### 8. Task 与 attempt 使用同一 claim fencing 事务

`ImageProcessingAttempt` 保存冻结策略、session、Executor attempt、最近可信累计金额、检查时间、提醒状态、终止原因、终止信号、`process_reaped` 和受控 `analysis_diagnostic`。该 JSONB 只允许固定检查阶段及三个稳定分析触发类别。Task 保存公开终态需要的错误及最近展示摘要。

Worker 把成功响应或失败异常中的有限字段写入内部 payload。`record_agent_attempt()` 在单一 Session 中锁定当前 Task 与 attempt，校验 scope、claim generation、lease owner、attempt 序号、配置摘要、workspace selector 和策略快照，然后同时更新两条记录。任何绑定不一致都会拒绝写回。

金额必须为非负有限数，检查时间必须包含时区，终止原因和信号必须属于固定集合。提醒状态只允许从未提醒变为已提醒。最终写入失败会将整个执行收束为 `unknown_execution`，防止外部执行已经发生后重复运行。

成功响应摘要在 `OpenCodeRunner` 中使用带锁的有限缓存跨越既有返回值边界。API 在调用结束的 `finally` 中读取并删除摘要，结果文件或 session 校验失败也会完成清理。缓存保留 5000 条上限，避免长期进程持续增长。

### 9. 恢复继续使用原策略和累计金额

恢复输入携带原 session、来源 attempt、处理配置摘要、workspace capability 和冻结策略。Executor 要求恢复策略与来源一致，并以持久化的最近可信金额作为最低值。读取到较小金额、session 冲突或 workspace 冲突时立即返回明确错误。

新 attempt 可以再次发送一次提醒，并读取原 session 继续累计的金额。旧 Worker 的迟到状态受到 claim generation 和 attempt 绑定约束，不能覆盖新 attempt 的金额、提醒状态或终态。本次调整面向新建任务，不迁移已有任务的金额记录。

### 10. 轮次继续作为独立观察信息

任务状态接口继续以只读方式读取 Agent 已完成轮次、进行中状态和最近活动时间。会话数据库不可读或 schema 不兼容时省略这些字段，正常任务状态仍然返回。

轮次观察与金额执行控制使用不同错误路径。轮次读取失败不改变冻结策略，也不阻止 Executor 根据累计金额停止任务。

## Risks / Trade-offs

- Executor 检查间隔允许已完成调用造成金额超过终止限额。提醒与终止之间保留足够距离，让 Agent 有时间收尾。
- OpenCode 的主 session `cost` 决定提醒和终止。供应商最终账单由现有计费系统结算。
- 任务拥有自身 OpenCode 数据库的写入权限，本功能依赖 OpenCode 正常记录用量。Executor 拒绝金额减少和非法值，但无法识别所有合法形式的数据库改写。
- 插件通过 Unix socket 报告提醒状态；Executor 的金额终止直接读取 session 数据。
- Agent 进程持有当前 attempt 的短期模型 capability。Server 必须限制 capability 的 attempt、模型、期限和接口范围。
- 插件检测完成前可能已经产生一次模型调用及费用。Executor 在确认失败后立即进入进程回收流程。
- PostgreSQL 摘要写回失败会失去安全重试依据。系统保留明确错误并使用 `unknown_execution`，不自动重放外部执行。

## Migration Plan

1. 公共核心增加策略、插件、Executor 读取、稳定错误映射和有限响应字段，默认保持关闭。
2. PostgreSQL revision 为 Task 与 Agent attempt 增加可空历史兼容字段，并为金额、终止原因和终止信号建立约束；后续 revision 为 attempt 增加固定值 `analysis_diagnostic`。
3. Server 合并已经审核并进入官方上游的精确公共 commit，再由模型目录生成启用策略。
4. 发布包含只读插件的 Agent 镜像，使用现有启动入口完成插件就绪、提醒、金额终止、进程回收和持久化验收。
5. 停止创建启用策略的新任务后，等待活动任务按已冻结策略结束；保留已经写入的策略和诊断字段，后续 revision 才能清理不再需要的数据。
