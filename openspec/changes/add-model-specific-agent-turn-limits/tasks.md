## 1. 公共分析用量策略

公共核心实现冻结策略、提醒插件、Executor 金额终止和持久化，默认关闭。Server 模型目录显式启用并提供三等级初始金额。插件门禁及金额执行控制只适用于冻结为启用的任务。

- [x] 1.0 实现公共默认配置同时关闭提醒插件和 Executor 金额终止；关闭时不要求策略或插件，保留现有超时和取消；启用状态随任务冻结
- [x] 1.1 在 Server 受控模型目录中配置 `model_free` 0.05/0.10 美元、`model_standard` 0.10/0.20 美元、`model_plus` 0.15/0.30 美元的提醒及终止限额，并让配置工具在未明确指定终止金额时使用提醒金额的两倍
- [x] 1.2 实现策略校验和解析，要求两个限额为有效正数且终止金额限额大于提醒金额限额，同时允许明确配置非两倍关系
- [x] 1.3 扩展任务提交协议，由可信模型目录冻结实际模型、变体、策略版本及两个明确限额；拒绝客户端或任务 payload 提交、提高或替换限额
- [x] 1.4 建立配置缺失、配置无效、`agent_analysis_reminder_plugin_unavailable`、`agent_analysis_usage_unavailable` 和 `agent_maximum_analysis_depth_exceeded` 等稳定错误，更新后端与 Executor client 的稳定错误集合并补充定向单元测试

## 2. OpenCode 提醒插件

- [x] 2.1 编写与生产 OpenCode `1.18.18` 匹配的本地 JavaScript 插件，通过当前 OpenCode 进程的 session SDK 投影读取冻结主 session 已完成请求的累计金额，不递归汇总子 session，也不自行重新计算 token 价格
- [x] 2.2 使用 awaited 系统提示钩子，在累计金额达到提醒限额后为当前 attempt 最多一次追加“尽快完成必要工作、验证结果并生成报告”的中文收尾提示
- [x] 2.3 以 attempt 状态记录提醒结果，覆盖该 attempt 内的请求重试、上下文压缩和后续模型调用；恢复产生的新 attempt 可以再次提醒一次
- [x] 2.4 对 session 缺失、用量读取失败和提示注入失败记录明确错误；运行期提醒失败允许任务继续，插件不调用终止流程
- [x] 2.5 让任务配置显式引用镜像内只读插件并传入冻结策略、目标模型、模型等级、attempt 标识和策略版本
- [x] 2.6 将插件源码和依赖预装到 Agent 镜像受保护路径，验证任务中的 Bash、Python 和 Node 不能修改或替换插件
- [x] 2.7 增加启动后就绪轮询测试，覆盖插件找不到、加载失败、初始化失败、版本不兼容、就绪超时、旧 attempt 就绪状态拒绝及未就绪即退出不得成功；覆盖主 session 单独计量、每 attempt 最多提醒一次、运行期提醒失败仍由 Executor 独立限制，以及任务级 bubblewrap 隔离边界

## 3. Executor 用量读取和终止

- [x] 3.1 以 Executor attempt 状态作为冻结策略、当前 session 标识、最近可信累计金额、提醒状态和最后检查时间的运行权威；Task 保存公开终态和最近展示摘要
- [x] 3.2 在现有进程检查循环中按冻结主 session ID 读取任务 `OPENCODE_DB` 的 `session.cost`，校验主 session 和工作目录绑定，并保留启动等待及金额不得减少的检查
- [x] 3.3 当观测金额达到或超过终止金额限额时，复用现有进程组 SIGTERM、有限等待、必要时 SIGKILL 和 wait 回收流程；无法确认回收时使用 `unknown_execution` 并保留原始触发原因
- [x] 3.4 接受 Executor 下一次检查前已经完成的一个或多个调用造成的金额超出，不增加流式 token 预测或请求期间金额截断协议
- [x] 3.5 将分析程度终止映射为 `agent_maximum_analysis_depth_exceeded` 和“超过最大分析程度”，同时在受保护诊断中保存阈值、最终观测金额、检查阶段和回收结果
- [x] 3.6 恢复同一 session 时校验并沿用原策略和累计金额，禁止进程、Worker 或服务重启后重置分析程度
- [x] 3.7 为任务数据库缺失、session 不匹配、schema 不兼容、金额减少、读取故障和金额字段无效建立明确执行控制错误
- [ ] 3.8 验证 Executor 主 session 金额读取及进程回收，覆盖低于限额、达到限额、调用后超过、插件失败、SIGTERM 正常退出、SIGKILL 回收、回收未确认的 `unknown_execution` 和旧 attempt 迟到状态

## 4. 后端持久化、任务状态和页面

- [x] 4.1 增加 PostgreSQL migration：Task 与 Agent attempt 保存冻结策略、累计金额、提醒状态、检查时间及终止诊断，attempt 保存进程回收结果和固定值 `analysis_diagnostic`
- [x] 4.2 扩展 Task 与 attempt repository 和 claim fencing 更新，只允许当前 claim 在同一事务中写入 attempt 权威事实及 Task 公开终态和摘要
- [x] 4.3 扩展 `ExecutorTaskResponse` 和 `AgentExecutorClient`，返回 `observed_cost`、`usage_checked_at`、`reminder_sent`、回收状态和受控终止摘要，不暴露金额限额、提示词、推理正文或凭据
- [x] 4.4 更新任务摘要和公开 DTO，对普通用户只返回 `agent_maximum_analysis_depth_exceeded` 和“超过最大分析程度”，内部诊断保留可行动的金额与终止阶段
- [x] 4.5 保留现有 `agent_completed_turns`、`agent_turn_running` 和 `agent_last_activity_at` 作为可选观察字段；轮次不参与提醒、剩余程度或终止判断
- [x] 4.6 更新前端任务列表和详情状态，明确显示“超过最大分析程度”，不显示内部提醒金额限额、终止金额限额或最终观测金额
- [x] 4.7 增加 repository、API、恢复、claim fencing 和前端测试，覆盖公开错误脱敏、内部诊断完整性、摘要缓存清理及轮次观察失败不影响终止

## 5. 集成验证和发布准备

- [x] 5.0 验证公共默认配置不要求策略、不加载插件、不执行金额终止，Server 显式启用，历史及创建时未启用任务行为不变，已启用任务在部署关闭后恢复仍执行原策略
- [x] 5.1 运行 `model_free` 0.05/0.10、`model_standard` 0.10/0.20、`model_plus` 0.15/0.30 初始策略、两倍默认关系、明确非两倍配置、策略冻结和错误映射单元测试
- [x] 5.2 运行 PostgreSQL migration、任务状态轮询、并发 claim、服务重启、session 恢复和旧 attempt fencing 集成测试
- [ ] 5.3 在 Docker Agent 环境执行真实任务，验证插件在每个 attempt 达到提醒金额后最多提醒一次、Executor 达到终止金额后停止，并接受下一次检查前已完成调用造成的金额超出
- [x] 5.4 验证插件缺失、损坏、初始化失败、版本不兼容或就绪超时会在启动后检测并触发停止；插件成功就绪后的提醒失败不会绕过 Executor；SIGTERM 未及时退出时 SIGKILL 和 wait 能够完成回收，回收未确认时不得报告普通金额或插件失败
- [x] 5.5 验证普通用户只看到“超过最大分析程度”，受保护日志保留观测金额、冻结策略、失败阶段和回收结果，且不包含凭据或推理正文
- [ ] 5.6 完成公共核心与 Server 的精确 SHA 同步、祖先关系核验、Docker 健康检查、Luna 价格检查及免费模型内部价格检查；Server 提交仅保存在本地
- [ ] 5.7 完成主 session 金额读取的严格复审、风险测试、静态检查和待用户审核的公共 commit
