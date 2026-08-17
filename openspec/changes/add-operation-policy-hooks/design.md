## Context

当前 scope-aware 公共核心已经通过请求 resolver 和持久 scope 事实区分业务归属，但上传、图片处理编排和反向图片 provider 边界还没有统一的可拒绝接口。图片上传存在普通 API 和合集导入两个入口；图片处理 Worker 在视觉结果有效后创建或复用持久 `meme_context_generation` Task；反向图片服务在缓存锁内决定是否真正联系外部 provider。宿主版若只包 HTTP 路由，会遗漏这些旁路和 Worker 恢复路径。

## Goals / Non-Goals

**Goals:**

- 提供与用户、订阅和支付无关的 operation vocabulary、policy 和 grant 生命周期。
- 把 policy 接入新图片 durable upload、图片处理 Worker 创建的逻辑 Agent Task 和真实联网反向图片调用的共同边界。
- 保持 dedupe、批量部分成功、缓存幂等、Worker 自动重试和任务 scope 传播的一致语义。
- 允许 policy 在 probe 后于真实副作用前再次原子判断，并在拒绝时不产生绕过副作用。
- 使用通用稳定原因和 `blocked` 状态表达执行限制，由 policy 可选提供 `retry_at`，核心不推断日、月或订阅周期，也不自动调度重试。
- 让开源 local 入口显式使用 allow-all，适配宿主只需注入 scope-aware 的 entitlement/quota 实现。

**Non-Goals:**

- 不实现 User、Account、Session、Subscription、Plan、Payment 或具体额度数值。
- 不把客户端身份、scope、余额、套餐或 grant 作为可信输入。
- 不重做已有反向图片缓存、审计、合集 ZIP 格式、Agent executor 或 scope 隔离设计。
- 不为每次 Worker attempt 单独计量，也不把普通读取、缓存命中或本地视觉搜索误算成付费 provider 调用。

## Decisions

### 1. 使用四阶段 policy 协议，而不是单次 `is_allowed` 回调

核心定义不可变的 operation request，至少包含可信 scope、稳定 operation 标识、服务端生成的幂等键、逻辑资源/任务引用、调用来源和请求单位；policy 返回不透明的 grant 引用或稳定拒绝原因。`probe` 只用于提前展示，`acquire` 原子预占，`commit` 确认已达到计量点，`release` 只处理确定没有发生副作用的 reservation。

只提供 `is_allowed` 无法防止并发请求同时通过，也无法表达上传失败补偿、Agent 任务恢复或 provider 调用幂等。把 quota 表直接放入开源核心则会把商业模型耦合到公共代码，因此 grant 的持久化和额度实现由宿主 policy 拥有，公共任务只保存服务端管理的不透明关联。

### 2. Operation 名称按业务能力命名

首期固定 `image.upload`、`analysis.agent`、`analysis.reverse_image_search` 和 `image.delete`。其中 `analysis.reverse_image_search` 只表示联网反向图片 provider 的逻辑调用；它不覆盖本地 `VisualSearchService`、有效缓存命中或通用网页搜索。使用 `analysis.reverse_image_search` 而不是 `analysis.network_search`，是为了避免把未来的文字网页搜索和当前 Google Lens 反向图片能力混为一谈。

### 3. 在公共流程的真实副作用边界接入 hook

- 普通上传和合集导入在图片校验完成、调用 durable storage 前 acquire；新 Meme 和文件持久化成功后 commit，明确未发生写入时 release。合集导入复用已有 Meme 不计量。
- 图片处理 Worker 先校验当前图片和有效 Agent 语境，再完成活动 `meme_context_generation` Task dedupe。确定需要新逻辑 Task 时，Worker 预生成服务端 task id 和稳定 `logical_request_key`，使用该 key acquire，并在可信持久化边界创建 Task 与 grant 关联；并发调用通过活动 Task 唯一约束和幂等 key 只取得一个 reservation。grant 与逻辑 Agent Task 关联，不与父 job revision、claim generation 或 execution attempt 绑定；每个 attempt 只引用该 Task/grant。
- grant 生命周期固定为：dedupe 后 acquire，在同一可信持久化边界关联 Agent Task，在 Agent executor 即将开始外部执行前 commit；只有能够证明外部调用未发生时才 release。acquire、commit 和 release 使用该 Task 的同一个服务端逻辑 request key。
- Agent Task 的自动 retry、租约恢复和 claim fencing 继续使用原 grant，不重新 acquire。每次外部 execution attempt 持久化不可猜测的 request/session 标识、输入摘要以及 `prepared -> grant_committed -> external_started -> completed` 状态。`prepared` 且 policy 仍为 reservation、并能证明调用未开始时可以 release；grant 已 commit 但 `external_started` 尚未持久化时，可以使用同一 Task/attempt/grant 开始首次调用；`external_started` 后缺少可验证结果时让 Task 以 `failed` 终态和 `unknown_execution` 错误码收束，对应图片处理阶段进入 `unknown_execution`，不得自动重放或 release。用户主动重试终态 job 时创建新 job revision 和新 Agent Task 并取得新 grant，旧 Task 和 grant 不重新激活。
- `ReverseImageService` 在缓存键锁内二次检查缓存。只有 miss 且准备联系 provider 时 acquire，并在 provider 边界 commit；同一 `request_id` 复用已有 usage/grant 事实。usage 必须在调用前持久化 `provider_started`。provider 明确返回失败、空结果或完整非法响应时记录稳定 provider failure；调用开始后因网络中断或进程退出而无法验证结果时记录 `reverse_image_unknown_execution`，保留 usage/grant 且不得再次联系 provider。`auto` Agent 可以在该子调用后继续离线分析，但不得把它提升为整个 Agent Task 或图片处理阶段的 `unknown_execution`。
- 删除在数据库或文件不可逆修改前 acquire，成功完成后 commit；删除不返还其他 operation 额度。部分失败只有在补偿确认所有不可逆修改均已撤销时才能 release；已有修改或无法证明无修改时保持 committed/unknown，并由恢复流程收束而不自动重放。

把 Agent hook 放在上传入口会为尚未通过视觉阶段的图片过早预占额度；继续放在视觉 handler 又会重新建立阶段耦合。由图片处理 Worker 在创建新的逻辑 Agent Task 时调用，既覆盖上传和图片库补齐，又保留稳定 `task_id`、`Task.scope_id` 和自动恢复的 grant 复用。把反向图片 hook 放在 provider 适配器内部则太晚，无法在缓存锁内解决 quota 竞态。

### 4. 拒绝与降级使用稳定、供应商无关的结果

策略永久禁止使用 `operation_forbidden`，可恢复或不可恢复的操作限制统一使用 `operation_limit_exceeded`，策略服务缺失或故障使用 `operation_policy_unavailable`；HTTP 映射由宿主适配层决定，但不得把 policy 原始错误、套餐内部字段或 provider 密钥传给客户端。policy 可以附带可选 `retry_at`，核心只校验、保存和返回这一提示，不计算它、不假设限制周期，也不在该时间自动重新排队。`analysis.agent` 被拒绝时，对应图片处理阶段进入通用 `blocked` 状态；只有用户显式重试或部署方受控恢复才能创建新的逻辑 Agent Task。`analysis.reverse_image_search` 在 `auto` 任务中被拒绝时，内部接口返回可继续的不可用结果，Agent 继续离线分析；不改为普通网页搜索。`forbid` 仍按既有策略直接拒绝。

策略和 provider 的异常都默认 fail-closed。allow-all 只由开源模块级应用工厂显式安装，宿主缺少 resolver 或 policy 时不回退到 local/allow-all。

operation policy 明确拒绝 `analysis.reverse_image_search` 时，`auto` 按 policy 语义继续离线分析。provider 未配置、内部服务不可用、完整协议错误或调用后结果未知则继续遵守反向图片 capability 自身的稳定失败或 `reverse_image_unknown_execution` 语义；其中未知子调用可以让 `auto` Agent 继续离线分析，但不能借 policy 层改写为成功，也不能污染整个 Agent Task 或图片处理阶段的状态。

### 5. Availability 查询只是提示

提供 scope-bound 的批量 capability 查询，返回 `available`、稳定原因和 policy 可选提供的 `retry_at`，不返回剩余额度或账户信息。所有真实入口仍必须 acquire；这避免 UI 查询与执行之间的竞态被误认为授权。`retry_at` 只用于展示或调用方决定何时显式重试，不是核心调度指令。宿主若需要详细用量，另行提供商业接口，不扩展公共 policy 契约。

## Risks / Trade-offs

- [Risk] policy 接入点遗漏会造成免费旁路。→ 对普通上传、合集导入、图片处理 Worker 的所有 Agent Task 创建入口、Worker 恢复、反向图片 provider boundary 做调用图审查和拒绝测试。
- [Risk] 外部调用在 commit 后进程崩溃会留下“已计量但结果未知”。→ 不 release 未知 grant，不自动重放同一付费调用；使用稳定 request/Task 幂等事实供人工或显式重试。
- [Risk] grant 关联写入失败导致 reservation 泄漏。→ Task/usage 持久化失败时执行幂等 release；宿主 policy 对过期 reservation 提供回收机制。
- [Risk] probe 结果被误当成授权。→ 在每个真实副作用边界强制 acquire，并测试并发最后一个名额。
- [Risk] 开源新接口改变现有 local 行为。→ 显式安装 allow-all，保留旧请求/响应字段，运行完整 local 和 PostgreSQL 回归。
- [Risk] operation vocabulary 与现有反向图片策略混淆。→ 保持 `reverse_image_policy` 的 `forbid/auto` 语义不变，仅新增独立的 `analysis.reverse_image_search` policy 层。
- [Risk] `retry_at` 被误认为核心保证的恢复时间或自动调度指令。→ 将它定义为 policy 提供的可选提示；执行仍须显式触发并重新 acquire。

## Migration Plan

1. 先完成 `make-application-scope-aware` 的 6.1-6.4；随后在同一个 keyword-only 应用工厂增加 policy 装配点，并由 local 入口显式安装 `AllowAllOperationPolicy`，不得建立第二套工厂或模块全局 policy。
2. 增加 operation request、grant 关联和稳定错误模型；先覆盖拒绝 fake policy、并发和幂等测试。
3. 将普通上传与合集新图片迁移到统一 upload policy 边界，验证失败补偿和批量部分成功。
4. 将图片处理 job 的有效产物检查、逻辑 Agent Task 创建、Worker retry/recovery 和 executor 起始边界迁移到 Task/grant 语义。
5. 在反向图片缓存锁内接入 `analysis.reverse_image_search`，验证 cache hit、provider_called、auto 降级和 request_id 恢复。
6. 接入 image.delete 和 capability probe，完成 local 全量回归后提交开源变更。
7. 将公共提交同步到宿主仓库，再由宿主实现认证 scope、entitlement/quota、reservation/usage 存储和 HTTP 错误映射。

回滚时先停止非 allow-all policy 流量；保留任务和 grant 关联，不把未完成的 non-local 操作映射为 local，也不删除已经产生的用量事实。
