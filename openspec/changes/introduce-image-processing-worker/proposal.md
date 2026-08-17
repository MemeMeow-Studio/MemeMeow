## Why

当前图片处理链由视觉任务隐式创建 Agent 任务、Agent 成功路径再触发 scope 级文本索引刷新，导致叶子任务承担编排职责，也让失败恢复、claim fencing、配额计量和“图片库一键补齐”难以使用同一套语义。需要把图片处理提升为独立的持久化能力，由专用 Worker 根据当前图片及其派生产物，只执行尚未完成或已经过期的阶段。

## What Changes

- 新增 scope-aware `ImageProcessingWorker` 和持久化图片处理 job，统一承接上传后的自动处理与图片库一键补齐。
- 将固定处理顺序定义为视觉向量 Task、Agent 语境 Task、单图文本 embedding Task；任一叶子 Task 失败即停止后续阶段，但保留图片和已经可靠提交的阶段产物。
- 每个阶段以当前 Meme SHA、模型、预处理、Agent skill/config 或 metadata hash 判断产物是否有效，重试和重新处理只执行第一个未完成阶段及其后续必要阶段。
- 保留 `visual_embedding_generation` 和 `meme_context_generation` 作为独立持久任务，并新增 `text_embedding_generation`；叶子任务不再互相创建后续任务，图片处理 Worker 成为唯一的任务创建、复用和阶段推进者。
- 将既有 `reverse_image_policy=forbid|auto` 冻结到图片处理 job revision 和 Agent Task；同图同策略的活动请求复用，不同策略保持 `generation_policy_conflict`，显式重试可以用新 revision 选择新策略。
- 新增按单图增量写入并立即参与当前 scope 搜索的 `text_embedding_generation`，全库 `cache_generation` 只保留为显式重建、迁移和修复能力。
- 上传成功后返回图片处理 job 标识，但上传自动流程不重启已有的终态失败 job；图片库一键处理按 scope 分页创建或复用逐图 job，将本次选择的反向图片策略传给每张图片，并把历史 `failed`、`blocked` 或 `unknown_execution` 视为用户显式重试，为其创建新的 job revision。
- Agent operation 被 policy 拒绝时，图片处理阶段进入通用 `blocked` 并保存 `operation_forbidden`、`operation_limit_exceeded` 或 `operation_policy_unavailable` 及可选 `retry_at`；核心不按提示时间自动重试。
- 专用 Worker 不沿用现有 `PostgresTaskWorkerManager` 的业务调度流程，但负责认领和执行上述三类图片叶子 Task，并复用经修复的数据库 claim、lease、generation fencing、scope 约束和资源并发原语；现有 Worker 继续处理其它系统任务。
- **BREAKING** 视觉任务成功不再自动创建 Agent 任务，Agent 成功不再自动触发 scope 级 cache generation；调用方应观察统一图片处理 job 及其阶段状态。

## Capabilities

### New Capabilities

- `image-processing`: 定义逐图处理 job、专用 Worker、阶段有效性、失败停止、policy 阻止、恢复与显式重试、图片库一键处理和部署生命周期。

### Modified Capabilities

- `image-ingestion`: 图片 durable 入库后改为创建或复用统一图片处理 job，而不是直接返回视觉任务作为处理链入口。
- `task-status`: 以图片处理 job 暴露总体与阶段状态及叶子 `task_id`，保留视觉与 Agent 任务的独立诊断能力，并移除叶子任务和批次 finalizer 之间的隐式推进责任。
- `meme-search`: 支持按单图 metadata hash 和 embedding 模型增量生成、更新或失效文本向量，并让有效向量无需全库 generation 即可参与搜索。

## Impact

- 主要影响 FastAPI 上传和图片库批量入口、任务状态 API、PostgreSQL job/stage 与文本向量 schema、Worker 生命周期、视觉/Agent handler、搜索 repository 和前端处理状态展示。
- 需要迁移现有任务链和搜索 generation 数据，并保留显式全库索引重建作为维护与回滚工具。
- `add-operation-policy-hooks` 必须改为在图片处理 Worker 准备创建或执行 Agent 阶段时处理 `analysis.agent` grant；上传、反向图片和删除的 operation 边界保持各自既有语义。
- 开源部署可在 API 进程内启动同一 Worker，宿主部署可把它作为独立进程或容器运行；两种模式共享数据库事实来源和恢复协议。
