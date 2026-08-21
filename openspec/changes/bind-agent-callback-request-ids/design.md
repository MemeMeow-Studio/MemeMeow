## Context

现有 callback 认证已经把请求绑定到当前 Task、scope、claim generation、attempt、operation 和目标 SHA，但 request 事实表只按 `(scope_id, request_id)` 唯一。反向图片服务在规范化请求前后都允许客户端提供 request ID，usage event 和 operation grant 又分别以该 ID 派生自己的幂等键；因此 callback 绑定字段正确，并不等于“同一逻辑检索只有一个请求事实”。

现有 `AgentCallbackRequest` 已持久化完整执行绑定和 `input_digest`，`ReverseImageUsageEvent` 已持久化缓存身份、provider started、结果未知和计量事实，operation grant 已有服务端 idempotency key/fingerprint 校验。`add-task-scoped-reverse-image-search`、`secure-agent-internal-callbacks`、`harden-operation-grant-association` 和 `add-operation-policy-hooks` 分别定义了这些基础能力；本设计只补足它们之间缺少的逻辑 request binding，不改变认证、provider、grant 生命周期或 task policy。

## Goals / Non-Goals

**Goals:**

- 让 callback 的逻辑身份由服务端规范化输入和当前执行事实决定，并在不同客户端 request ID 之间保持唯一。
- 在首次创建、并发竞态、缓存锁等待、进程崩溃和 provider 结果未知时复用同一 callback/usage/grant 事实。
- 在不丢弃历史副作用事实的前提下，用现有持久表和数据库约束建立可证明的唯一边界。
- 保持现有 callback token、Task claim、反向图片参数、薄 CLI、Host/Docker/executor 运行时和无 callback local 夹具兼容。

**Non-Goals:**

- 不新增用户、账户、订阅、计费、quota owner、IP 限流、在途限制或新的 operation vocabulary。
- 不把缓存键改造成 callback 授权键；缓存命中、provider 调用和 operation grant 继续由各自既有事实表达。
- 不让新 attempt 继承旧 attempt 的执行权，也不把未知 provider 结果自动变成可重试的已知成功。
- 不通过删除、合并或猜测改写历史 usage、grant、callback 或缓存记录来“修复”重复事实。

## Decisions

### 1. 复用 callback 事实表，以现有输入摘要建立逻辑唯一键

不新增 `logical_request` 实体。服务端在完成 `ReverseImageRequest` 的图片、目标和参数规范化后，继续生成 64 位 `input_digest`，但该摘要只由服务端可信的当前 binding、实际图片 SHA 和规范化 `search_type`、`language`、`country`、`query`、`auto_crop`、`refresh` 组成；客户端提供的 digest 只作为可校验的重复声明，不能覆盖计算值。

在 `agent_callback_requests` 上增加覆盖 `scope_id`、Task、claim generation、attempt、operation、target SHA 和 `input_digest` 的复合唯一约束/索引。已有字段已经足以表达执行+目标+规范化输入，不另建表或把 grant 复制进 callback 记录。内存 repository 使用同样的双索引语义：按 request ID 查改绑，按逻辑事实查权威 ID。

选择现有表和唯一约束，而不是仅在 Python 字典中查找，是因为多个 API 进程和 Worker 可能同时进入 provider；选择复合索引而不是把完整请求 JSON 存入新表，是为了让 scope、claim 和输入摘要继续由现有审计事实负责。

### 2. 采用“request ID 兼容提示、逻辑身份权威”的解析顺序

callback 请求进入反向图片服务后按以下顺序解析一次权威请求：

1. 认证和当前 claim 校验先于 Task 业务查询、缓存、usage、grant 和 provider；随后读取目标 Meme/源图片并产生规范化输入摘要。
2. 若客户端提供 request ID，先按当前 scope 查找该 ID。命中时必须逐项比较完整逻辑事实；不一致立即返回既有 `usage_request_conflict`，不执行任何业务副作用。
3. 再按复合逻辑唯一键查找已有 callback 事实。命中时，无论客户端是否更换 ID，都使用存量行的权威 request ID；如果 usage 已有终态，直接恢复其结果；如果 grant/provider 状态未知，沿用既有 `unknown_execution`。
4. 两个索引都未命中时，首次请求提供的合法 ID 可以作为权威 ID；省略 ID 时由服务端根据稳定逻辑摘要生成确定性 ID。插入遇到并发唯一冲突时重新读取逻辑事实并采用竞争者的权威 ID，不能换一个新 ID 再插入。

这种解析同时保留旧 CLI 的显式 ID 兼容性、允许新 CLI 省略 ID，并确保“更换 ID”只能成为一个安全的事实查找，而不是新的副作用入口。operation grant 的 `reverse:<request_id>` 和 usage event 的 request ID 都只在此步骤之后使用。

### 3. 保留规范化参数的结果语义，单独纳入 refresh

缓存 `identity()` 继续按既有 provider、图片 SHA、搜索类型、语言、国家、query 和受控裁剪参数工作，`refresh` 仍表示跳过可复用快照。逻辑 request digest 额外纳入 `refresh`，因此同一 `refresh=true` 请求重复提交会命中相同权威事实，而 `refresh=false` 与 `refresh=true` 不会错误共享 provider 已计量事实。

规范化必须发生在摘要计算前：文件名只用于格式判断，查询外围空白和空值按现有模型收束，国家和语言按现有大小写/长度限制处理，布尔字段使用严格值。服务端裁剪后的实际图片 SHA 和原始 Task 目标 SHA 都属于可信输入；Agent 不能以任意裁剪图片、source SHA 或自报 digest 改变逻辑身份。

不在本 change 中改变既有 64 位摘要的外部形状，避免破坏 PostgreSQL 字段、usage 关联和宿主适配器。若实现需要升级摘要序列化，必须先提供旧摘要兼容读取并单独记录版本，不能让新算法把旧事实变成“未发生”。

### 4. 将 callback、usage 和 grant 的恢复边界串成一个顺序

反向图片服务使用 callback repository 返回的权威 ID 重建 `ReverseImageRequest`，再进入现有缓存锁和 usage repository。缓存锁内仍二次检查 Task 状态/策略和缓存；缓存命中只写或恢复无 provider usage 事实，不 acquire `analysis.reverse_image_search` grant。miss 或明确 refresh 只以权威 ID 构造 operation policy idempotency key，并复用现有 grant association 状态门禁。

provider started 必须先持久化 usage 事实；provider、缓存快照、usage 终态和 callback 终态的写入继续使用既有 unknown 协议。不同 request ID 不能创建第二个 callback row，因此也不能到达第二个 grant key。对于旧安装中只有 usage 记录而 callback 事实不完整的请求，服务端只能按已有 request ID 的完整一致性读取；无法确认逻辑归属时 fail-closed，绝不通过猜测建立别名。

### 5. 让持久约束和内存夹具具有相同并发语义

PostgreSQL migration 在新代码启用前创建复合唯一索引，并在迁移事务中先扫描重复逻辑事实。若历史数据已有两个不同 request ID 占据同一逻辑键，迁移必须报告冲突并停止，不能自动选一条、删除另一条或重写 provider/usage/grant；这样保留事实并阻止不完整 schema 被误认为安全。

内存 repository 无法提供数据库锁，因此只作为单进程语义夹具：先按 request ID 和逻辑键查找，再在同一锁内插入；测试必须额外用 PostgreSQL 两个独立 session 验证数据库唯一约束是真实并发门禁。callback 路径在必需 schema/index 未就绪时 fail-closed，不回退到 request ID-only 的旧逻辑。

### 6. 错误映射沿用已有稳定协议

- 无凭据、旧 claim、scope/target/attempt 无效继续使用 `agent_callback_unauthorized` 或 `agent_callback_invalid_execution`，且发生在逻辑键查找前。
- 已有 request ID 与新输入冲突、同一逻辑键的持久事实无法安全解析，使用已有 `usage_request_conflict` 或等价 callback conflict，响应不泄露其他 Task/scope 的存在性。
- usage 已记录 provider started 但结果不可确认时使用 `reverse_image_unknown_execution`；重复请求只读该事实，不通过新 ID 降级为普通 retry。

不新增“request ID 被替换”专用外部错误码；这样 Host、Docker、executor 和薄 CLI 不需要分叉错误处理，仍可消费已有供应商无关 JSON/错误映射。

### 7. 运行时兼容只改变 request ID 的权威来源

薄 CLI 保留现有 `--request-id` 可选参数；默认不发送它，原样打印后端返回的权威 ID。Runner、Host、Docker 和 executor 的环境变量、callback token、内部 URL、任务 ID 和 secret 隔离不变。无 callback binding 的 local 直连调用不创建 `agent_callback_requests`，继续使用原有显式 request ID、缓存和 refresh 语义，以免把本地测试/开发夹具误当作 Agent 协议。

## Risks / Trade-offs

- [Risk] 存量数据已经存在同一逻辑键的多个 request ID，唯一索引无法直接建立。→ 迁移先做重复扫描，发现冲突即停止并保留所有事实；发布门禁要求明确恢复/审计处理后再启用 callback。
- [Risk] 旧版本摘要与新规范化输入的表示不同，可能无法自动识别旧逻辑事实。→ 保留现有摘要形状和兼容计算路径；任何无法证明等价的旧行都 fail-closed，不用新 ID 重放 provider。
- [Risk] 并发请求在 callback 事实保存后、usage 保存前退出。→ 重试先解析同一权威行，再按 canonical ID 创建/恢复 usage；provider started 或 grant 状态未知时遵循现有未知协议。
- [Risk] 将 refresh 纳入逻辑键而缓存键不含 refresh，可能让一次 refresh 和一次普通命中拥有不同审计行。→ 这是有意的：缓存仍可复用，provider/计量事实按实际逻辑请求区分，测试分别断言 cache hit 与 provider call 计数。
- [Risk] composite index 让每个 callback 插入承担额外写放大。→ 索引字段均为现有 bounded 标识/摘要，callback 本身已经需要持久事实；不添加第二套大 JSON 或全局锁。

## Migration Plan

1. 在兼容窗口先读取当前 `agent_callback_requests` 的 binding/input digest 完整性，并统计 `(scope, task, claim, attempt, operation, target, input_digest)` 重复组；任何重复组都阻止 schema migration，不自动合并。
2. 安装只向前的复合唯一索引，并更新 schema revision/启动期 schema 检查。新 callback 实现只在该索引可用时启用；普通 local 直连不依赖该 callback 表。
3. 发布 callback resolver 和 ReverseImageService 修改：先规范化并解析权威 ID，再进入现有缓存、usage、operation grant 和 provider unknown 流程；同步增加内存、PostgreSQL 和 API/CLI 兼容测试。
4. 在 Host、Docker、executor staging 中运行重复 ID、不同 ID 同输入、query/refresh、旧 claim、重启和密钥隔离回归；不改变 callback token 或环境变量协议。
5. 回滚时停止 Agent callback 流量或回到同时能读取该索引的旧版本，保留唯一索引和全部 usage/grant/callback facts；不得删除索引后重新开放 request ID-only 的 callback，也不得自动重放未知 provider。
