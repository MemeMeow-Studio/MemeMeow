## 1. 持久化模型与数据库控制面

- [x] 1.1 定义图片处理 job、固定三阶段到叶子 Task 的持久关系、规范化 `reverse_image_policy`、必要的执行尝试、`unknown_execution` 阶段诊断及 scope 级增量向量迁移状态的数据库模型、枚举、约束和迁移。
- [x] 1.2 为 job 目标签名、metadata/context revision、活动 job 与叶子 Task 去重、阶段查询、scope 隔离和单图文本向量检索建立精确唯一约束；文本向量唯一键固定为 `(scope_id, meme_id, image_sha256, metadata_hash, embedding_model_version)`，job-stage-task 跨表关系使用复合 scope 外键。
- [x] 1.3 抽取或等价实现任务与图片 job 共用的 PostgreSQL 认领、租约、heartbeat、过期恢复和 claim-generation fencing 原语；每次 claim 原子递增 generation。
- [x] 1.4 实现图片 job repository 的创建或复用、按 scope 查询、阶段状态转换、主动重试创建新 job revision、上传自动流程不重启终态失败和目标变化收束，所有写回均校验 scope、目标 SHA、lease owner、claim generation、stage 和 attempt；受影响行数为 0 时统一视为 fencing rejection。

## 2. 专用图片处理 Worker

- [x] 2.1 实现 `ImageProcessingWorker` 的启动、停止、job reconcile 和有界并发调度；让它成为三类图片 Task 的唯一扫描与认领者，并让 `PostgresTaskWorkerManager` 明确排除这些类型及旧批次 finalizer。
- [x] 2.2 实现视觉产物有效性校验及 `visual_embedding_generation` Task 的创建、复用、专用执行、原子产物写回和失败诊断；视觉 Task 不创建 Agent Task。
- [x] 2.3 实现 Agent 语境有效性校验及 `meme_context_generation` Task 的创建、复用、专用执行、结果解析校验和原子写回；将 job 的 `reverse_image_policy` 冻结到 Agent Task，保留 usage 摘要和 Meme provenance，Agent Task 保留稳定 `task_id` 并不创建文本任务。
- [x] 2.4 实现 `text_embedding_generation` Task 的创建、复用、专用执行、metadata hash 构造、维度校验和单图向量原子写入。
- [x] 2.5 为视觉、Agent 和 embedding 配置独立的有界资源预算；保留跨进程 Agent lane，并保证扫描和短事务不占用 Agent slot。
- [x] 2.6 将外部 attempt、request/session id 和输入摘要可信关联到对应叶子 Task；恢复只能查询并采纳同一 task/attempt，无法证明安全时让 Task 以 `failed` 终态和 `unknown_execution` 错误码收束，并让父 job 阶段进入 `unknown_execution`。

## 3. Operation Policy 与恢复边界

- [x] 3.1 在图片处理服务中定义可注入的 operation policy 调用点，并在创建新的 `meme_context_generation` 逻辑 Task 时关联 `analysis.agent` grant；按 acquire、Task 关联、commit、确定未调用才 release 的顺序执行；拒绝时让阶段进入通用 `blocked` 并保存稳定原因和可选 `retry_at`。
- [x] 3.2 保证 Agent Task 的自动 retry、租约恢复和 claim generation 变化复用既有 grant；`blocked` 及其 `retry_at` 不触发自动重试，图片库一键处理或单图主动重试终态 job 时创建新的 job revision 和必要的新 Agent Task 并重新获取 grant，旧 job/Task 不重新激活。
- [x] 3.3 保持开源默认 policy 始终允许，并确保视觉和文本 embedding 不会隐式消耗 `analysis.agent` grant。

## 4. 增量文本搜索与迁移

- [x] 4.1 实现按当前 scope、Meme、图片 SHA、metadata hash 与 embedding 模型过滤的文本向量查询和稳定去重排序。
- [x] 4.2 使图片或可 embedding 语境变化立即令旧向量失去搜索资格，并让新向量提交后立即参与搜索。
- [x] 4.3 实现按 scope 的 `legacy_only`/`backfill`/`incremental_only` 迁移状态、epoch、旧 `cache_generation` 逐条校验的只读回退、增量向量补齐进度和原子切换；一次查询只能选一种来源，禁止过期 generation 混入增量结果。
- [x] 4.4 保留显式 `cache_generation` 的维护入口，并移除它对日常图片处理链和单图搜索资格的写入影响。

## 5. API、状态与旧链切换

- [x] 5.1 修改上传成功路径：在 Meme 和图片持久化后通过服务端解析的 scope 与规范化 `reverse_image_policy` 创建或复用图片处理 job，并返回 job 标识；同一目标签名已有终态失败时保留原诊断，不自动创建重试 revision。
- [x] 5.2 修改图片库一键处理入口：分页枚举当前 scope 的 Meme，把本次规范化 `reverse_image_policy` 逐图传给显式重试模式；有效或活动 job 按同策略复用/异策略冲突，历史 `failed`、`blocked` 或 `unknown_execution` 创建新 revision，并支持部分成功与独立状态。
- [x] 5.3 提供图片处理 job 的 scope-safe 状态查询和主动重试接口，返回总体、阶段、叶子 `task_id` 和有限诊断；保留统一 Task 查询与 Agent 活跃度，`unknown_execution` 必须可见，主动重试返回新 job revision 标识。
- [x] 5.4 移除视觉完成后自动创建 Agent、Agent 完成后自动创建 cache generation，以及视觉/Agent 批次 finalizer 的旧推进路径。
- [x] 5.5 将专用 Worker 接入应用生命周期；保证 Worker 只从持久 job 的 `scope_id` 恢复 scope，正常关闭时停止新认领并安全收束执行权。

## 6. 数据迁移、可观测性与文档

- [x] 6.1 实现现有图片的分页 job seed 与增量 embedding 回填工具，不将全库图片 ID 放入单个任务 payload。
- [x] 6.2 增加 job、阶段、attempt、lane、unknown execution、grant acquire/commit/release、fencing 拒绝及迁移进度的结构化日志和受限指标，不记录 Agent 原文、密钥或跨 scope 数据。
- [x] 6.3 更新部署、恢复、回滚和显式全库重建文档，明确新旧搜索切换条件与未知外部执行的人工处置方式。

## 7. 验证

- [x] 7.1 为数据库模型和 repository 增加迁移、精确唯一键、复合 scope 外键、伪造 scope/path/stage/grant、目标 SHA 或 metadata 变化、租约过期、旧 claim 写回和并发认领测试。
- [x] 7.2 为 Worker 增加三类 Task 唯一执行控制面、阶段顺序、task_id 关联、有效产物/Task 复用、任一 Task 失败停止、`reverse_image_policy` 冻结及同策略复用/异策略冲突、自动 retry 与显式重试区分、服务重启恢复和旧结果晚到测试。
- [x] 7.3 为 Agent Task grant 增加 acquire/commit/确定未调用才 release、自动恢复不重复获取、主动重试新 Task 重新获取、policy 拒绝后 `blocked`、可选 `retry_at` 仅提示以及 unknown execution 不自动重放测试。
- [x] 7.4 为增量搜索增加向量立即可见、语境或图片变化立即失效、embedding 写回 CAS、维度失败隔离、scope 过滤、迁移状态单一来源、旧 generation 逐条校验及显式 `cache_generation` 不覆盖新向量测试。
- [x] 7.5 为上传、图片库一键处理、状态查询和重试接口增加端到端测试，覆盖上传不重启终态失败、一键处理显式重试历史失败、单图 `blocked` 不影响其它图片、跨 scope 枚举或查询、并发重复请求、伪造客户端字段、语境更新与 embedding 竞态和关闭恢复。
