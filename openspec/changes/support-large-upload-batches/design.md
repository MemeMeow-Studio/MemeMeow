## Context

当前 `/images/upload` 已有逐文件校验、StorageCoordinator durable operation 和图片处理 job 去重，但 multipart 入口一次读取全部文件且没有公开上传能力配置。前端上传工作区把一次选择绑定到一个请求，只能在请求结束后整体处理结果。详见 proposal.md 及本 change 的 delta specs。

## Goals / Non-Goals

**Goals:**

- 在现有 Meme、storage operation 和图片处理 job 事实之上实现有界请求与响应丢失后的幂等认领。
- 让前端以最多 20 文件、最多 2 并发的逻辑调度处理大批量选择，并安全处理中止、暂停和 429 背压。
- 让服务端以配置响应公开客户端所需边界，同时保留默认 disabled 的总字节预算扩展点。

**Non-Goals:**

- 不新增持久 upload session、batch 表、跨请求断点续传或服务端批次恢复协议。
- 不把开源默认改为 64 MiB 总请求限制，不修改 Server 仓库，不同步或推送任何远端。
- 不改变单图片处理阶段状态机、scope 隔离和现有逐文件部分成功语义。

## Decisions

### 1. 请求边界由 multipart 解析和逐项 spool 读取共同执行

上传入口调用 Starlette multipart parser 的文件数上界（允许解析一个额外文件以返回稳定业务错误），并在 parser 写入 spool 时按文件 part 实际字节校验可选总预算。预算和文件数校验在 parser 返回前完成，不产生 durable 写入；随后从 `SpooledTemporaryFile` 逐个读取文件。每个文件最多读取 `max_upload_size + 1` 字节，当前内容处理完后才读取下一项，避免把整个请求复制到 `preloaded` 列表。这样不依赖 `Content-Length`，chunked 请求也受同一边界约束；没有总预算时仍执行单文件 20 MiB 上限。

相比只检查 Content-Length，这能处理缺失或不可信的头；相比引入自定义 multipart parser，复用框架解析器并把边界集中在入口，改动面更小。Starlette spool 负责承接请求体，业务层只保留当前单文件上限内容，后续可在独立变更中继续降低单文件处理内存。

### 2. 上传配置使用启动期 Settings 字段并通过 `/config` 脱敏暴露

新增 `max_files_per_request`（服务端约束为 20）、仅供客户端调度的 `max_concurrent_upload_requests`（默认提示为 2）和可选 `max_request_bytes` 字段，均有 Pydantic 上界校验。`Settings.status()` 返回非敏感整数或 `None`；默认总预算为 `None`。服务端只重新执行文件数和总字节边界，不把并发提示作为在途请求 admission。

相比让前端读取硬编码常量，这避免部署预算与切片逻辑漂移；相比把预算放在处理任务配置中，上传预算不影响图片产物指纹。

### 3. 幂等重试先验证 durable 三方事实，再读取已有处理 job

读取每个文件后先规范化文件名并计算 SHA-256/大小。若目标文件不存在，继续现有 operation 上传；若目标存在，则在当前 scope 按 storage key 查询 Meme，并同时验证数据库记录与实际文件的大小、SHA-256。只有三方一致才返回既有 Meme；此路径不 acquire upload operation。通过现有 `ImageProcessingRepository.latest_for_target` 读取当前图片版本最新 job，响应其状态；无 job 时才提交普通 pipeline job，提交仍由既有 scope/图片指纹去重保证。

相比按文件名直接返回成功，该设计能防止孤立文件、数据库损坏或被替换文件被错误认领；相比新增 idempotency 表，复用当前不可变 Meme 和 processing job 事实，满足不新增持久实体的边界。不同内容返回冲突，事实不一致返回 reconciliation 错误。

### 4. 前端使用纯调度 composable 和轻量逐项视图

新增 `useUploadBatch` composable 保存最小状态：文件项、状态、结果、运行请求数、暂停和取消信号。纯函数负责按文件数/可选字节预算切片；单个文件大于预算时单独成片，确保循环前进。调度器只创建不超过公开并发提示（客户端默认 2）的 fetch，结果按分片索引写回；模板用稳定 item key 和 `v-memo`，进度变化不重建未变化的行。

`api.upload` 接受可选 AbortSignal，`request` 将 429 的 `Retry-After` 转成错误元数据。调度器对 429 暂停派发并延迟恢复，对网络/5xx 保留可重试失败，对格式、大小、文件名和冲突等错误保留永久逐项失败。取消只 abort 当前客户端请求和标记本地未发送项，不调用删除或回滚接口。

相比新增 Pinia 全局 store，上传状态只属于当前工作区且页面刷新本来就丢失本地文件；composable 更小且可以直接进行 Vitest 纯函数/行为测试。相比虚拟列表，引入 `v-memo` 和轻量行渲染足以覆盖当前几千项范围，避免在本变更引入新的 UI 依赖。

## Risks / Trade-offs

- **[单文件处理内存峰值]** multipart spool 承接完整请求，业务层每次只读取一个文件且最多保留单文件 20 MiB 加一字节 → 不会按文件数线性复制请求内容；后续可将单文件处理改为受控临时文件流。
- **[旧客户端硬编码]** 旧客户端可能继续发送超过 20 个文件 → 服务端权威拒绝并返回稳定错误，前端读取 `/config` 后按边界切片。
- **[Retry-After 日期解析]** 代理可能返回不规范值 → 客户端对无效值使用短退避并保留失败项，绝不扩大并发；服务端不依赖该客户端行为。
- **[幂等处理选项差异]** 重试请求可能带不同处理选项 → 既有 job 状态优先返回，option 冲突只作为处理状态诊断，不重放 durable 上传；用户仍可使用现有显式阶段重试。

## Migration Plan

1. 部署包含新 Settings 字段和 `/config` 字段的服务；未设置 `max_request_bytes` 时行为保持 disabled。
2. 部署前端调度器；旧客户端仍可按旧入口提交不超过 20 个文件，超过边界由服务端拒绝。
3. 回滚时移除前端调度器和新增配置读取即可；不需要数据库迁移，已有 Meme、storage operation 和 processing job 不变。

## Open Questions

无。默认预算、文件数和并发数已由产品约束确定。
