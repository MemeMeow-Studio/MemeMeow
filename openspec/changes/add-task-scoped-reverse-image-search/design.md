## Context

当前语境任务把图片和固定 prompt 交给 OpenCode。共享 Agent 容器拥有开放网络及通用命令行工具；Runner 在全局配置 SerpApi 时把供应商密钥交给每个任务。Skill 中的 Google Lens 脚本同时承担 CLI、文件缓存、并发锁、供应商访问和脱敏，文档还保留可绕过脚本的直接 `curl` 调试方式。

任务已经以 PostgreSQL JSONB 持久化输入和结果，并按 `scope_id` 隔离；当前产品固定使用 `local` scope，尚无用户实体。Agent 容器可访问图片只读挂载和 runtime 写目录，但不持有数据库凭据。详见 [proposal.md](proposal.md) 的变更动机。

## Goals / Non-Goals

**Goals:**

- 让任务 payload 成为 `forbid`/`auto` 策略的唯一事实来源。
- 让 Agent 只能通过稳定的项目接口使用已配置的反向图片供应商，且拿不到供应商密钥。
- 在缓存并发、任务重试和进程故障下可靠地区分缓存使用与实际供应商调用。
- 以当前 `scope_id` 作为用量归属，为未来映射到用户或租户保留数据边界。
- 保持 Agent 的 CLI 调用和统一 JSON 契约不依赖具体供应商。

**Non-Goals:**

- 本期不增加登录、用户表、角色、任务 token、接口签名或其他认证机制。
- 本期不承诺阻止开放网络中的 Agent 向任意第三方发送图片；保证范围是供应商密钥不进入 Agent，项目反向图片能力受任务策略控制。
- 本期不增加额度限制、收费、购买套餐或用量结算。
- 本期不强制 `auto` 任务调用以图搜图，也不增加第三个“必须调用”档位。

## Decisions

### 1. 策略在 API 边界进入并随任务冻结

`reverse_image_policy` 使用字符串枚举 `forbid | auto`。上传表单、单图 JSON、批量 JSON 和重试请求都显式携带它；批量请求在顶层选择一次并应用到本批次各项。`_context_payload` 始终写入规范化值，Worker 只读取 payload，不读取当前前端状态或全局环境推断策略。

缺省值选择 `forbid`，既兼容历史任务，也避免部署方配置了供应商密钥后无意扩大图片外发范围。提交 `auto` 时后端先检查供应商是否配置；未配置直接拒绝，避免“允许但永远不可用”的静默降级。

活动任务仍以图片身份和内容指纹互斥。去重查询同时比较策略：同策略复用，不同策略返回 `generation_policy_conflict`。没有选择把策略简单加入唯一去重键，因为那会允许两种策略同时处理并覆盖同一份图片语境。

### 2. 增加无认证的内部接口，不把供应商密钥交给 Agent

新增供应商无关的 `POST /internal/reverse-image/search`，第一版放在现有 FastAPI 服务中并依赖当前受控部署的信任边界，不实现认证。请求使用 multipart：

- `task_id`
- `image`（整图或 Agent 生成的受限裁剪）
- `search_type`
- `language`
- 可选 `country`、`query`、`auto_crop`、`refresh`

接口通过 `task_id` 查询数据库，只接受处于 `running` 的 `meme_context_generation`，并从任务 payload 读取策略。调用方不提交 scope 或 policy，因此以后可以在接口前增加认证或从身份上下文解析 scope，而无需改变检索参数和响应主体。

Runner 给 Agent 的只是内部接口地址与已有的任务标识。这里使用环境变量只传递运行时连接信息，不承载业务策略。任何策略下都不再注入 `SERPAPI_API_KEY`。`forbid` 同时写入 prompt 约束；真正的标准能力边界由内部接口再次检查任务记录。

备选方案是继续由脚本直连供应商并在结束后上报事件。它改动较少，但密钥仍暴露给 Agent，直接 HTTP 调用可以绕过缓存和计数，因此不采用。完整任务 token 认证能够加强隔离，但当前单 scope、受控 Agent 的收益不足以覆盖复杂度，留待信任边界变化时在接口背后增加。

### 3. Skill 脚本缩减为内部接口的薄 CLI 客户端

保留现有命令名、图片和搜索参数，脚本改为：解析参数、读取内部接口地址与任务标识、提交 multipart、打印统一 JSON、把稳定错误写到 stderr。脚本删除 `.env`/`SERPAPI_API_KEY` 读取、供应商 URL、共享缓存写入和供应商响应脱敏职责。

Skill 文档删除生产和 Agent 可见的直接 `curl` 供应商调用方式，只指导调用薄客户端。后端接口保持：

```json
{
  "request_id": "...",
  "cache": {"key": "...", "status": "hit", "fetched_at": "...", "outcome": "success"},
  "provider": {"called": false, "outcome": "success"},
  "result": {}
}
```

Agent 不依赖 SerpApi 字段；以后替换供应商时，由后端适配器把候选规范化到同一 `result` 契约。第一版只需要一个 SerpApi 实现和清晰的服务边界，不提前建立插件注册系统。

### 4. 缓存与供应商访问迁入后端服务

把现有内容哈希、参数规范化、缓存 TTL、同键文件锁、原子快照和脱敏逻辑迁入后端反向图片服务。继续使用部署已配置的持久缓存根目录和兼容的缓存 schema，避免上线后全部冷启动。

服务在同一缓存键锁内二次检查缓存。有效命中直接返回；未命中后创建请求事件，再调用供应商。一次逻辑检索可能包含本地图片上传和 Lens 查询两个 HTTP 步骤，但对产品计数始终是一次。空结果和调用失败只要已经开始联系供应商也计一次；这是“实际尝试使用付费能力”的稳定口径，不依赖供应商是否成功返回候选。

### 5. 用量事件是计数和审计的单一事实来源

新增 `reverse_image_usage_events` 表，建议字段为：

- `id` / `request_id`：全局唯一，承担幂等；
- `scope_id`、`task_id`、`meme_id`：归属与回查；
- `cache_key`、`cache_status`：缓存路径；
- `provider_called`、`provider`、`outcome`、`retryable`：调用与结果；
- `created_at`、`provider_started_at`、`completed_at`：时序。

每次内部接口请求都有事件；缓存命中的 `provider_called=false`，实际供应商调用开始前原子地写成 `provider_called=true`。当前 scope 的调用次数由 `provider_called=true` 的事件计数得到，而不是再维护一个可能漂移的汇总字段。未来引入用户时，可把 scope 映射到 principal，或在不改变事件语义的前提下增加 `principal_id`。

供应商失败不得删除事件。请求重试使用同一 `request_id` 时复用记录，数据库唯一约束防止重复计数。当前 CLI 每次命令生成一个请求 ID；服务端仍是 ID 的权威校验和持久化方。

### 6. 任务终态由后端汇总审计，Agent 不自报

任务结束或失败时，任务服务按 `task_id` 聚合 usage events，生成：

```json
{
  "reverse_image": {
    "policy": "auto",
    "attempted": true,
    "used": true,
    "cache_hits": 1,
    "provider_calls": 0,
    "outcome": "success"
  }
}
```

`attempted` 表示是否请求内部接口；`used` 表示是否得到可供研究使用的缓存或供应商结果；`provider_calls` 只统计实际开始的逻辑供应商检索。任务成功写回 Meme 时，同一摘要进入 provenance；任务失败时仍写入 Task.result，避免调用发生后因 Agent 失败而丢失审计。

## Risks / Trade-offs

- [Risk] 无认证接口允许知道活动 `auto` 任务 ID 的调用方借用该任务发起检索。→ 当前明确接受受控单 scope 部署的信任边界；接口仍校验任务类型、运行状态和策略，契约预留以后在路由背后加认证。
- [Risk] Agent 仍有开放网络，`forbid` 不能被解释为禁止所有可能的图片外发。→ UI 和文档只承诺禁用项目反向图片能力；供应商密钥不进入 Agent。若未来要求强隔离，再增加出口网络策略。
- [Risk] 从脚本迁移缓存代码可能改变缓存键或导致冷缓存和额外费用。→ 固定现有 request identity 与 schema 作为回归样本，读取已有脱敏快照，并通过离线缓存测试验证兼容。
- [Risk] 服务在供应商请求开始和事件状态提交之间崩溃，结果可能长期停留在 started。→ 调用开始前先持久化事件，恢复时保留为已计数的未知结果，不自动重放付费请求。
- [Risk] 同一任务可能进行整图和裁剪检索，用户看到的次数大于任务数。→ 任务详情同时展示实际调用数，计数口径按逻辑检索而非任务说明。
- [Trade-off] 从事件表实时聚合比单个自增字段查询更贵。→ 当前调用规模很小且事件是可靠事实来源；需要用量面板时再增加可重建的汇总视图或计数表。

## Migration Plan

1. 先新增 usage events 表、索引和只向前迁移，部署后端接口及服务，但暂不移除脚本直连路径。
2. 更新 API 请求、任务 payload、去重规则、Runner prompt 与内部接口连接信息；历史缺失策略按 `forbid`。
3. 切换薄客户端和 Skill 文档，确认 Agent 环境不再包含 SerpApi 密钥，内部接口缓存兼容测试通过。
4. 更新前端四类入口和任务审计展示，再启用 `auto` 选择。
5. 回滚应用代码时保留新增表；旧应用忽略新增 payload/result JSON 字段。若薄客户端已上线而后端接口回滚，临时禁用 `auto`，不得重新把供应商密钥交给 Agent。
