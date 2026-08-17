## Context

现有 `ImageProcessingWorker` 以持久化图片处理 Job 编排视觉向量、Agent 语境和文本 embedding 三个叶子 Task。这个模型正确地隔离了编排与执行，但现有前端只会提交图片级处理，通用 Task 重试端点又可能绕过 Job 直接复制叶子 Task 的 payload。详见 [proposal.md](proposal.md) 的 Why，以及既有 `image-processing`、operation policy、scope 和 Agent callback 约束。

## Goals / Non-Goals

**Goals:**

- 让完整处理和仅处理一个阶段成为可持久化辨识、可安全去重的不同提交模式。
- 保留专用图片 Worker 对三类图片叶子 Task 的唯一执行权，以及既有 claim、lease、generation fencing、scope、SHA、grant 与 callback 校验。
- 让任务页从服务端事实而非 Task 类型或前端猜测构建 Job 层级和独立任务视图。
- 让独立阶段重试不意外产生下游模型调用或受限操作消耗。

**Non-Goals:**

- 不改变完整 Job 的固定阶段顺序、阶段有效性、失败停止或恢复策略。
- 不引入通用 DAG、任意阶段组合或“从指定阶段开始并自动执行所有下游”的第三种用户操作。
- 不允许客户端选择 scope、Job 归属、Task 归属、Worker、claim、grant、callback 或图片文件路径。
- 不把历史任务的诊断、Job revision 或既有成功产物重写为新的独立任务历史。

## Decisions

### 1. 用持久化提交来源区分两种用户操作

新建的三类图片 Task 增加受约束的持久化 `submission_mode`：`pipeline` 或 `standalone`。`pipeline` Task 必须有一条既有图片 Job 阶段关系；`standalone` Task 必须没有该关系。Task 类型继续表达阶段类型，`submission_mode` 表达谁拥有编排责任，两者不得互相推断。数据库约束、创建服务和迁移都必须防止同一个 Task 同时成为两种来源。无法可信回填的历史 Task 保持来源为空，并在读取模型中标记为未归类历史诊断项；它不是第三种用户提交操作。

不建立第二张独立阶段任务表：通用 `Task` 已经是持续执行、claim、活动去重和诊断的权威身份，新增平行表会复制这些事实并引入同步风险。Job 关联仍由既有阶段关系承载；来源字段只补齐“无 Job 的图片 Task 是有意独立提交”的最小事实。

历史图片叶子 Task 在迁移时根据可靠的 Job 阶段关系回填为 `pipeline`；无法可靠关联的历史记录以未归类只读诊断项处理，不能伪装为独立任务。通用 Task 重试对所有图片阶段 Task 一律拒绝，避免迁移缺口成为绕过入口。新的独立 Task 必须经专门图片阶段提交服务创建。

### 2. API 按操作语义分流，通用 Task 重试不再承担图片阶段重试

完整 Job 的创建、查询和重试继续通过图片处理 Job API 进行。上传和批量处理仍只调用完整 Job 提交服务；对终态 Job 的“完整重试”必须创建新的 revision，同一重试操作的并发重复请求才可复用该活动新 revision，并由 Job reconcile 按阶段有效性推进。新增受限的单图、单阶段提交 API，只接受服务端允许的阶段标识和受校验的业务选项（Agent 的 `reverse_image_policy`）；它返回独立 Task。独立阶段提交也只复用相同的活动 Task，终态 Task 必须以新的逻辑 Task 重试。

`POST /tasks/{task_id}/retry` 在目标为视觉、Agent 或文本 embedding 图片阶段 Task 时始终拒绝，无论其历史来源为何。这样不会因遗漏 Job 关联或伪造 Task payload 把内部叶子转成独立任务。调用方需要“仅重试”时一律以当前 scope 的 Meme 标识和目标阶段重新提交；服务端重新读取当前图片、SHA、配置和权限，而不信任历史 Task payload。

替代方案是给 Job 重试增加 `from_stage`。该参数表达“继续编排下游”，无法表达“只执行一个阶段”，会在用户只想刷新视觉向量时创建不必要的 Agent 与文本任务，因此不采用。

### 3. 专用 Worker 按提交来源决定是否推进父 Job

`ImageProcessingWorker` 继续是三类图片 Task 的唯一扫描、认领和执行控制面。执行前和结果写回前均使用 Task 的持久 scope、当前 Meme、SHA、阶段配置和 claim generation 校验。

对 `pipeline` Task，完成、失败、阻止或目标变化后唤醒对应 Job reconcile；只有该 reconcile 能创建后续阶段。对 `standalone` Task，Worker 只完成该 Task 和本阶段产物，绝不创建或唤醒完整 Job，也绝不调度其他阶段。两类任务使用独立的活动去重键，至少区分 scope、Meme、图片 SHA、阶段、模式、配置和 Agent 策略，因而一个活动 Job 子任务不会被误复用为用户的独立重试。

独立 Agent Task 仍在“新的逻辑 Agent Task”边界获取 operation grant，并沿用服务凭据 callback verifier、attempt、callback、usage 与 unknown-execution 收束协议。它成功写入新语境时只使文本向量的来源签名失效，供后续显式独立文本重试或完整 Job 处理；该失效是允许的结果一致性更新，不是文本阶段执行，且不得唤醒或触发既有 Job reconcile。视觉向量不作为 Agent 或文本阶段的输入版本，独立视觉 Task 不使这两个阶段失效或重跑。

### 4. 状态 API 返回显式层级，前端不通过推测拼装任务

图片处理 Job 状态继续返回固定三个阶段及其叶子 Task 标识。Task 状态和列表响应增加或统一返回可安全公开的 `submission_mode`、图片阶段和可选 `processing_job_id`；未归类历史 Task 返回明确的只读历史诊断标记而非伪造的提交模式。Job 关联只作为当前 scope 内的不可猜测资源引用返回；跨 scope 查询使用与未知资源相同的结果。

处理任务工作区以 Job 为父项渲染 `pipeline` Task，以 Task 为单项渲染 `standalone` Task。图片库操作显式调用完整提交或单阶段提交 API，并在获得对应 Job/Task 标识后轮询其各自状态。界面不传递内部归属字段，且在活动去重、policy 拒绝或目标变化时显示服务端诊断而不是乐观创建本地任务。

### 5. 以追加式迁移和双读展示保护现有任务历史

先为现有图片 Task 和 Job 阶段关系补充来源字段及查询索引，再回填可验证的 Job 子任务关联。新 API 与 Worker 只写入新来源事实；在回填完成前，任务查询将缺少来源的历史图片 Task 作为未归类只读历史诊断项展示，但不得开放通用重试。部署完成并完成回填后，对新建 Task 的来源和 Job 关联启用约束。

回滚时停止新单阶段提交路由和前端入口，保留新增来源字段、独立 Task 和 Job 历史。完整 Job 的既有处理、叶子 Task 诊断和产物保持可读；不得通过删除独立 Task、grant 或 callback attempt 来回滚。

## Risks / Trade-offs

- [独立 Agent 更新语境后文本向量暂时不可检索] → 明确使其失效但不自动重建，向用户展示文本阶段可单独重试，避免未请求的模型调用。
- [独立任务与 Job 子任务并发写同一阶段产物] → 两类任务复用现有图片 SHA、输入版本、数据库锁、CAS 和 claim fencing；结果只在仍匹配当前输入时采纳。
- [历史叶子任务缺少可靠的父 Job 关联] → 历史项以未归类只读诊断展示且禁用全部重试；新的处理从当前图片重新派生安全输入。
- [新增 UI 操作使重复提交增加] → 后端按模式分离活动去重，前端依据返回的 Job/Task 标识禁用重复操作。
- [Agent 独立重试误绕过计费或联网限制] → 仍通过 operation policy 取得新的可信 grant，且 callback 必须匹配 Task、scope、SHA、attempt 与当前 claim。

## Migration Plan

1. 增加图片阶段 Task 来源、阶段和可选 Job 关联的持久化约束与查询索引；回填能由阶段关系可靠证明的 `pipeline` Task。
2. 抽取完整 Job 和独立阶段提交服务，收紧通用 Task 重试；为独立 Agent 接入现有 policy/grant 和 callback 约束。
3. 更新图片 Worker，使其按 `submission_mode` 执行同一阶段 handler，但仅对 `pipeline` 结果 reconcile 父 Job；补充 Agent 语境更新后的文本向量失效逻辑。
4. 更新 Job/Task API 响应与图片库、处理任务工作区，使用显式模式渲染与轮询。
5. 在迁移数据、并发提交、跨 scope、过期 claim、Agent policy 拒绝、callback 重放和阶段无下游调度场景下验证；逐步启用新 UI 操作。
