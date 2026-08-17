## 1. Policy 契约与应用装配

- [x] 1.1 新增 operation policy 公共模块，定义稳定 operation 标识、可信 operation request、probe/acquire/commit/release 结果和不透明 grant 引用；客户端字段不得进入可信上下文
- [x] 1.2 实现 `AllowAllOperationPolicy`，并在开源 local 应用入口显式装配；缺失、未知或异常的非 allow-all policy 必须 fail-closed
- [x] 1.3 定义 `operation_forbidden`、`operation_limit_exceeded`、`operation_policy_unavailable` 等稳定错误及宿主可映射的错误载荷；支持由 policy 提供可选 `retry_at`，核心不得计算限制周期或泄露 policy 原始错误、scope 和商业字段
- [x] 1.4 为公共服务和任务提供 server-managed 的 grant 关联持久化方式；关联不得出现在客户端可覆盖的普通 payload 中，并保证 acquire/commit/release 幂等

## 2. 可用性查询与上传边界

- [x] 2.1 增加按当前 scope 查询首期 operation 可用性的服务/API 能力；probe 不得建立 reservation，响应只允许可用状态、稳定原因及可选 `retry_at`，不得包含余额、套餐内部字段或 grant
- [x] 2.2 在普通上传校验完成和 durable upload 开始之间接入 `image.upload` acquire，并在文件与 Meme 持久化成功后 commit、确定无副作用时 release
- [x] 2.3 让合集导入的新图片复用同一 `image.upload` 生命周期；复用已有 Meme 不 acquire，并保持批量项目独立成功或失败
- [x] 2.4 覆盖上传 policy 拒绝、并发最后名额、存储失败补偿、durable 状态未知不 release、非法输入不预占和批量部分成功测试

## 3. 图片处理 Worker 的 Agent Task 计量

- [x] 3.1 在图片处理 Worker 确认当前图片没有有效 Agent 语境并完成活动 Agent Task dedupe 后，以服务端预生成 task id 或稳定 `logical_request_key` acquire `analysis.agent`，并发竞争通过幂等 key 和活动 Task 唯一约束只建立一个 grant/Task
- [x] 3.2 将 grant 与 `meme_context_generation` Task 和稳定 request key 可信关联持久化；execution attempt 只引用 Task/grant，关联失败时只释放可证明未使用的 reservation，客户端不得提交或覆盖关联
- [x] 3.3 在 OpenCode 外部执行开始前幂等 commit；对未开始且可证明无副作用的失败幂等 release，并记录稳定阶段错误
- [x] 3.4 让 Agent Task 的自动 retry、租约恢复和 claim fencing 复用同一 Task/grant；持久化 attempt 的 `prepared`、`grant_committed`、`external_started`、`completed`/`unknown_execution` 状态、request/session id 和输入摘要，并按每个崩溃窗口执行恢复矩阵；未知执行时 Task 为 `failed` 且错误码为 `unknown_execution`，父 job 阶段状态才是 `unknown_execution`
- [x] 3.5 让用户主动重试终态 job 时创建新 job revision、新 Agent Task 并重新 acquire；旧 job、Task 与 grant 保持终态
- [x] 3.6 明确视觉和单图文本 embedding 不消耗 `analysis.agent` grant，未来需要计量时使用独立 operation
- [x] 3.7 增加有效产物与活动 Task 零计量、并发创建同一 Task 仅一次 acquire、Agent 拒绝后进入 `blocked` 且图片保留、`retry_at` 不触发自动重试、commit 前后各崩溃窗口、自动恢复不重复 acquire/commit、未知结果不重放、主动重试新 Task/grant 和客户端伪造 grant/job/task/attempt 的测试

## 4. 联网反向图片检索

- [x] 4.1 将 operation 标识固定为 `analysis.reverse_image_search`，明确排除本地视觉搜索、缓存命中和普通网页搜索
- [x] 4.2 在反向图片缓存键锁内完成二次缓存检查，并在 miss 且准备联系 provider 前 acquire；provider 开始时 commit
- [x] 4.3 将 grant/request_id 与既有反向图片 usage 事实关联，在 provider 调用前持久化 `provider_started`；明确失败、空结果或完整非法响应记录稳定 provider failure，调用开始后无法验证结果则记录 `reverse_image_unknown_execution`，并保证恢复和重复提交不重复计量或调用
- [x] 4.4 在 `auto` 策略被 policy 拒绝时返回稳定不可用原因和可降级结果，使 Agent 继续离线分析；`forbid` 保持直接拒绝
- [x] 4.5 增加缓存命中零计量、并发同键单次 provider、operation 限制降级、provider 未配置保持既有失败语义、明确 provider failure 保留计量、调用开始后网络中断或进程退出返回 `reverse_image_unknown_execution`、`auto` 继续离线分析但不污染 Agent Task 状态，以及 request_id 幂等测试

## 5. 删除与安全边界

- [x] 5.1 在删除文件、Meme、向量、任务和合集关联前接入 `image.delete` acquire，成功完成不可逆副作用后 commit；部分失败仅在完全补偿且可证明无副作用时 release，否则进入 committed/unknown 并由恢复流程收束
- [x] 5.2 验证删除不返还其他 operation 额度，覆盖文件失败、数据库提交失败、补偿成功/失败，以及跨 scope 资源和伪造 grant 不触发 policy 或业务副作用
- [x] 5.3 完成生产路径固定 `local`、客户端 scope/用户字段、grant 泄露和内部 Agent/反向图片旁路的静态扫描与回归测试

## 6. 验证与同步准备

- [x] 6.1 补充 fake deny/allow policy、并发 reservation、幂等 commit/release 和策略故障 fail-closed 的单元测试
- [x] 6.2 运行 scope-aware 变更后的完整 Python、PostgreSQL、任务恢复和反向图片测试，确认开源 allow-all 的 API/前端行为不变
- [x] 6.3 更新公共 API/架构文档，说明 operation vocabulary、probe 非权威性、grant 不可伪造和宿主 policy 注入边界
- [x] 6.4 执行 `openspec validate --strict`、`compileall`、`git diff --check`，记录未具备外部 provider/Docker 条件的验收缺口
