## Context

现有流程以通用任务 Worker 执行视觉和 Agent 任务，并由视觉 handler 隐式创建 Agent 任务、由 Agent handler 或批次 finalizer 触发 `cache_generation`。这使叶子 handler 同时承担业务计算和流程推进，且当前文本搜索仅能通过 scope 级全库 generation 生效。详见 [proposal.md](proposal.md) 的 Why。

现有 `tasks` 控制面已具备 PostgreSQL 原子认领、租约、claim generation fencing、活动去重和跨进程 Agent lane。`Task.scope_id` 是叶子执行及 Agent 内部 callback 的持久化授权边界；请求上下文和 payload 不得成为 Worker 的 scope 事实。新流程必须保留视觉与 Agent Task 及这些安全属性，但不能继续让叶子 handler 或 `TaskBatch` finalizer 承担阶段编排。

## Goals / Non-Goals

**Goals:**

- 建立一个面向单张图片的持久化处理 job 与专用 `ImageProcessingWorker`，使上传和图片库补齐走同一条可恢复路径。
- 让视觉、Agent、文本 embedding 保持为独立持久 Task，并由一个明确的图片 job 编排事实按顺序创建、复用和观察，而不是由叶子 handler 互相提交任务。
- 为文本 embedding 建立按图片版本、metadata hash 和模型版本生效的查询存储。
- 复用并抽取已有的数据库并发控制原语，维持多进程、重启、租约过期和跨 scope 写回下的正确性。
- 为 operation policy 提供稳定的“新的逻辑 Agent Task”边界，避免自动恢复重复扣量。
- 区分上传触发的自动处理和图片库一键处理：前者不重启终态失败，后者是用户对所选 scope 图片的显式重试。

**Non-Goals:**

- 不引入通用 DAG、任意工作流定义语言或跨图片的父子任务引擎；该 Worker 只支持固定的三阶段图片流程。
- 不在开源版实现用户、账户、订阅、支付或具体额度规则。
- 不删除显式 `cache_generation` 维护能力，也不把它作为日常图片链的隐式后续操作。
- 不使客户端提交的 `scope_id`、`user_id`、路径、job 状态或内部回调标识成为授权凭据。

## Decisions

### 1. 使用专用图片处理 job 编排独立叶子 Task

新增 `image_processing_jobs` 及其阶段关系记录。每个 job 冻结 `scope_id`、`meme_id`、目标图片 SHA-256、规范化 `reverse_image_policy` 和处理配置指纹；每个阶段关系保存阶段输入版本、对应叶子 `task_id`、状态和有限诊断，文本阶段还冻结 metadata hash 和语境版本。叶子任务继续使用现有 `Task` 控制面：视觉为 `visual_embedding_generation`，Agent 为 `meme_context_generation`，文本为新增 `text_embedding_generation`。job 的总体状态由阶段和叶子 Task 事实导出或在同一事务内更新，不能仅存于 JSON payload。终态 job 的复用必须重新对照当前阶段有效性；metadata/context hash 或反向图片策略变化时创建新的 job revision，而不是重新激活旧 job。

`ImageProcessingWorker` 同时承担短事务编排和三类图片叶子 Task 的专用执行控制面。一次 job reconcile 只检查产物与叶子 Task 状态，并创建或复用当前唯一可运行的叶子 Task，不持有父 job claim 等待模型执行。叶子 Task 由专用 Worker 使用既有 claim/lease/fencing 语义执行；任务完成后唤醒或由扫描器重新 reconcile 父 job。现有 `PostgresTaskWorkerManager` 不再注册、扫描或认领这三类图片任务，只继续处理 `cache_generation`、`metadata_repair` 等其它系统任务。

替代方案是把父 job 塞入 `tasks` 并由 handler 阻塞等待子任务。它需要为 `waiting` 重新定义现有任务状态并长期占用 Worker 资源。另一个被否决的方案是取消叶子 Task、让 job 直接执行全部模型阶段；这会丢失 Agent `task_id`、任务活跃度、内部 callback 的 `Task.scope_id` 事实和既有 claim/retry 契约。复用 `TaskBatch` 也不成立，因为它没有有序依赖，且会在成员失败后继续收束。

### 2. 抽取而非复制租约、认领和 fencing 控制面

将现有 PostgreSQL claim/lease/heartbeat/claim-generation 模式抽取为专用 Worker 和现有 Worker 共用的窄数据库原语。图片 job 的短事务 reconcile 与三类叶子 Task 的认领都必须使用事务锁和跳过已锁行；任何任务进度、终态、产物写入、阶段关系更新和后续 job 唤醒都必须匹配 `Task.scope_id`、lease owner 和 claim generation，并核验父 job scope 与目标版本。

任务类型必须有唯一执行控制面：`visual_embedding_generation`、`meme_context_generation`、`text_embedding_generation` 只由 `ImageProcessingWorker` 扫描和认领，现有 Worker 明确排除这些类型。路由使用服务端固定的 task-type ownership 集合，不接受 payload 或客户端选择 Worker。

视觉、Agent 和 embedding 分别配置资源预算。Agent 阶段继续使用跨进程的 Agent lane；视觉和 embedding 使用独立的有界预算，避免任一种工作耗尽另两种资源。job worker 的扫描与短事务不占用 Agent lane。

替代方案是新建内存队列或在每个 scope 启动一个 Worker。前者无法在重启后恢复且容易跨进程重复，后者会放大线程与并发上限并错误依赖默认 scope。

### 3. 阶段有效性优先于历史任务状态

Worker 每次推进时在短事务内重新读取目标 Meme、图片 SHA-256 与对应产物。阶段是否需要运行由以下事实决定：

- 视觉阶段：目标 SHA、视觉模型和预处理版本匹配的有效视觉向量；
- Agent 阶段：目标 SHA、语境结果版本、Agent 配置和冻结的 `reverse_image_policy` 匹配的有效语境；
- 文本阶段：目标 SHA、由白名单语境字段计算的 metadata hash 与 embedding 模型匹配的有效文本向量。

连续有效的前置阶段直接标记为复用；第一个缺失或过期阶段变为唯一可运行阶段。目标删除、scope 不匹配或 SHA 变化时 job 进入 `target_changed`，不允许依据旧路径写入任何产物。成功阶段之后的输入发生变化会创建或复用新的目标签名 job，而不是把旧 job 的成功状态改写为新图片成功。

终态失败、`blocked` 或 `unknown_execution` job 不得被上传后的自动 enqueue 重新激活或自动替换为新 revision。图片库一键处理属于用户显式重试：它为这些终态 job 创建新的 revision，重新校验并复用仍有效的连续前置阶段，从第一个失败、阻止、未知或过期阶段继续。这样既避免上传、进程重启或 `retry_at` 到达时形成无限重试，也让历史失败图片可以通过同一个图片库入口恢复。

替代方案是以“最近一次任务成功”作为依据。任务记录无法证明结果仍对应当前图片、配置和语境，会在替换图片或变更模型后错误复用。

### 4. Agent 外部副作用使用逻辑阶段与执行尝试分离的恢复协议

图片处理 job 是外部可见的编排单位，`meme_context_generation` Task 是逻辑 Agent 工作单元和内部 callback 的执行身份。Worker 在确认不存在有效语境或可复用活动 Agent Task 后，预生成服务端 task id 和稳定 logical request key，并按 `operation-policy` capability 创建带可信 `analysis.agent` grant 关联的 Agent Task。grant 的 acquire/commit/release、崩溃窗口和计量语义只由该 capability 定义；本 change 只规定 Worker 的去重、Task 创建、阶段状态和恢复集成点。

每次 Agent 外部执行 attempt 在可信 Task 元数据或关联记录中持久化不可猜测的 attempt/request/session id、输入摘要、scope、目标 SHA、冻结的 `reverse_image_policy` 和 Agent 配置，并引用该 Agent Task 的 grant。进程在外部调用后、结果写回前失联时，恢复者只探测同一 Task/attempt 的结果，并在结果完整、目标和 claim 约束均满足时采纳；不同 Task、attempt 或 session 的结果不得被冒认。无法证明调用未发生或无法采纳结果时，Agent Task 以 `failed` 终态和稳定 `unknown_execution` 错误码收束，父 job 的 Agent 阶段进入 `unknown_execution`，不得静默重复付费或联网调用。文本 Task 可以使用同类 attempt 记录防止未知副作用重复，但不消耗 `analysis.agent` grant。

Worker 必须在有效语境和活动 Agent Task 去重后进入 operation policy 生命周期；同一 Agent Task 的自动 retry、lease recovery 和 claim generation 变化复用原 Task/grant，`failed`、`blocked` 或 `unknown_execution` job 的用户显式重试则创建新 revision 和必要的新 Agent Task。不得在上传时预取 grant，也不得为 Worker attempt 自行定义第二套计量或恢复协议。

Agent acquire 返回 `operation_forbidden`、`operation_limit_exceeded` 或 `operation_policy_unavailable` 时，不创建或执行 Agent Task，父 job 的 Agent 阶段进入通用 `blocked` 并保存稳定原因和 policy 可选提供的 `retry_at`。`retry_at` 只用于状态展示和调用方决定何时显式重试；Worker 不建立定时唤醒，也不在日期变化或提示时间到达后自动创建 Task。开源 `AllowAllOperationPolicy` 不产生这种 policy 阻止状态。

### 5. 文本搜索改用按图片版本的增量向量

新增单图文本向量存储，精确唯一键为 `(scope_id, meme_id, image_sha256, metadata_hash, embedding_model_version)`，向量维度和算法版本由固定配置约束或一并纳入有效性版本，并保存向量、维度、创建时间和来源版本。旧版本向量可以保留为不可检索历史。写入与将其标记为可检索在同一事务完成；文本 provider 返回后，写回事务必须再次锁定或读取当前 Meme 语境并以 metadata hash、scope、图片 SHA、模型版本和 claim 做 CAS；不匹配时阶段只能标记 stale/reconcile，不能标记成功。语境更新入口必须触发可被 Worker 发现的 reconcile，避免旧 hash 写回后没有新 job。

`cache_generation` 保持独立的显式维护路径。它不再由图片阶段创建，也不能覆盖新向量的有效性。每个 scope 持久化 `legacy_only`、`backfill`、`incremental_only` 等迁移状态和 epoch；一次查询只能选择一个来源。迁移期间使用旧 generation 回退时，必须逐条 join 当前 scope 的 Meme 并验证图片 SHA、metadata hash、模型、维度、语境状态和文件可访问性；无法证明有效的条目排除。图片或语境更新与切换并发时，更新在同一 scope epoch 下进入新 job，切换事务不能混入旧 epoch。切换后日常查询只读取增量向量。回退窗口必须受持久化迁移状态保护，禁止把语境已变化的旧 generation 当作当前向量。

替代方案是继续把每个 job 的文本阶段接到共享全库 `cache_generation`。这会重新引入全库屏障、频繁重建和“某次 generation 是否包含本图”的不可判定关系。

### 6. 入口、状态与旧自动链一次切换

上传、单图处理、图片库一键处理和显式重试都继续接受既有 `forbid|auto` 业务选项，服务端负责规范化，缺失时使用 `forbid`。上传成功后只创建或复用图片处理 job 并返回其 ID；同一图片和配置已有活动 job 时，同策略复用，不同策略返回 `generation_policy_conflict`，不能并行创建两个可能覆盖同一语境的 Agent Task。如果同一目标签名只有终态失败、`blocked` 或 `unknown_execution` job，上传自动流程返回现有诊断而不创建重试 revision。图片库一键处理按页读取当前 scope 的 Meme，并为每张图片调用同一服务的显式重试模式：有效或活动 job 按策略规则复用，历史终态失败则以本次选择的策略创建新的 job revision。该服务由服务端根据已解析 scope 和数据库目标构造 job，不接受客户端提供的 scope、重试模式或阶段状态；`reverse_image_policy` 是受校验的业务选项，不是授权凭据。

状态 API 将图片处理 job 作为独立资源公开，返回总体、阶段状态和对应叶子 `task_id`；原有 Task 状态接口继续提供视觉、Agent、文本 embedding 的独立进度、活跃度和诊断。视觉 handler 内创建 Agent、Agent handler 内创建 cache generation、以及 `TaskBatch` 对视觉/Agent 的 finalizer 必须在同一发布中移除，防止新旧链同时推进；删除的是隐式推进责任，不是叶子 Task。

替代方案是长时间双写双调度。它会造成重复 Agent 调用、重复扣量和不同状态来源相互覆盖，因此仅允许在不调度旧后续阶段的只读迁移窗口内保留兼容读取。

## Risks / Trade-offs

- [新增 job 与阶段表提高了数据库模型复杂度] → 保持固定三阶段，禁止在本变更中抽象通用 workflow；用唯一约束、外键和状态约束表达最小事实。
- [外部调用后的进程崩溃可能留下未知结果] → 保存执行尝试；优先采纳可验证结果，无法证明安全时停止并要求显式恢复，不能通过自动重放换取表面可用性。
- [增量向量迁移期间搜索可能暂时不完整] → 以 scope 迁移状态控制只读旧 generation 回退，后台补齐后原子切换；对没有可验证回退和新向量的 scope 返回明确未就绪。
- [大量图片库补齐产生突发负载] → 分页 seed、活动 job 去重、每阶段独立有界资源预算与数据库背压；不得把全库 ID 列表放进单个 payload。
- [图片库一键处理重复消耗受限 operation] → 仅为终态失败创建一个新 revision；仍有效的阶段和活动 Task 继续复用，新的 Agent Task 必须重新经过 policy acquire，单图被阻止不影响其它图片。
- [新 Worker 错误复用不同反向图片策略] → 将规范化策略冻结到 job revision、Agent Task、去重比较和结果 provenance；活动策略冲突显式拒绝，不通过建立第二个任务解决。
- [旧 Worker 或延迟执行者可能写入新链] → 删除旧自动推进路径，并以 scope、目标 SHA、lease owner 与 claim generation 共同 fencing 所有写回。
- [操作策略实现尚未落地] → 本变更定义注入点与逻辑阶段边界；在 `add-operation-policy-hooks` 修订并实施前，开源默认 policy 始终允许，部署版不得启用计费策略。

## Migration Plan

1. 先完成 `make-application-scope-aware` 的 6.1-6.4，并在同一个 keyword-only 应用工厂装配 scope factory、operation policy 与 callback verifier/issuer；再增加图片处理 job、阶段到叶子 Task 的关系、必要的执行 attempt 和单图文本向量 schema，以及索引、精确唯一约束、复合 scope/目标外键。保留现有 `tasks`、`TaskBatch` 和已激活 cache generation 数据。
2. 发布能读取新状态但尚不调度新 job 的代码，验证迁移、租约恢复、跨 scope fencing 与搜索读路径；为每个 scope 建立持久化的向量迁移进度。
3. 分页为现存图片创建或复用图片处理 job；历史任务缺失 `reverse_image_policy` 时按 `forbid`，并保留已有 Task 反向图片 usage 摘要和 Meme provenance。生成并校验增量文本向量；在某个 scope 的现存图片均达到可验证状态后，将该 scope 的搜索读取原子切换到增量向量。
4. 启用上传与图片库补齐的 `enqueue_or_reconcile`，启动专用 Worker，将三类图片 Task 的唯一认领权从现有 Worker 切换给专用 Worker，并在同一发布中删除视觉到 Agent、Agent 到 cache 及批次 finalizer 的自动推进。
5. 保留显式 `cache_generation` 作为维护工具并观测 job 失败、unknown execution、claim fencing 拒绝、阶段耗时和每个 lane 的背压；确认稳定后再清理不再被读取的旧自动链数据。

回滚时先停止新 job 的认领和新入口调度，保留所有新表与已写入的阶段产物；仍有有效旧 generation 的 scope 可以切回只读旧搜索。没有可验证旧 generation 的 scope 必须返回未就绪，而不是查询可能过期的数据。不得在回滚中删除 job、文本向量或执行尝试记录。

## Open Questions

- 文本 embedding provider 的成本是否需要在宿主部署中单独计量，留待定义新的 operation vocabulary 后决定；这不改变本变更中其不复用 `analysis.agent` grant 的边界。
