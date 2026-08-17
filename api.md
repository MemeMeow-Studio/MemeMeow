# MemeMeow API

FastAPI 是唯一业务入口。模型密钥、Base URL 和路径只从服务端 `.env` 读取，前端不能修改配置或获得绝对路径。

## Scope 与部署

应用工厂必须显式注入可信 scope resolver：

```python
from api import create_app
from backend.scope import LocalScopeResolver

app = create_app(scope_resolver=LocalScopeResolver("local"))
```

模块级 `api:app` 入口已经显式安装上述 local 适配器；适配宿主应从已完成认证的请求上下文提供自己的 resolver，可按需通过 `service_factory` 与 `agent_input_provider` 注入 scope 服务和受控 Agent 输入，也可以省略 `service_factory` 使用核心默认 factory。只要 resolver 不是显式 `LocalScopeResolver("local")`，默认或宿主 factory 启动都不会调用 `for_scope("local")`、执行 local storage preflight 或创建 local 业务 service；默认 factory 在请求或任务认领后按可信 scope 懒创建 facade，宿主数据库可以没有 local scope。漏配 resolver、resolver 返回空值/非法值或 scope 服务装配失败时，应用或请求以稳定的 `scope_resolution_failed` / `scope_unavailable` 错误失败，不回退到 `local`。

请求中的 `scope_id`、`user_id`、路径前缀和普通业务字段不会改变 resolver 结果。上传、媒体、图片元数据、搜索、合集和公共任务 API 只访问当前请求 scope；属于其他 scope 的资源统一按不存在处理，不返回其存在性、文件路径或物理 namespace。

任务创建时服务端把请求 scope 写入不可为空的 `Task.scope_id`，payload 不承担授权 scope。Worker、重试、视觉/反向图片 callback 和 Agent 子任务从持久任务或有效 claim 恢复 scope；两个既有 `/internal/...` callback 不依赖用户 request scope。宿主 non-local scope 必须提供受控 `agent_input_provider`；未配置或返回符号链接/非普通文件时任务稳定失败为 `agent_input_provider_unavailable`。内部 callback 由 API 独立验证短期任务凭据，网络隔离和内部路由暴露范围仍由部署宿主负责；本 API 不新增用户登录或 scope 管理接口。

进程内只运行一个 scope-aware Worker manager，所有 scope 共享线程池、handler registry、lane 背压、claim owner 和恢复扫描；请求侧 task facade 不启动 scope 专属 Worker。

## 检索

### `POST /search`

请求体：

```json
{"query":"开会时忘记准备材料","n_results":8,"llm_enhance":false}
```

`query` 必须非空，`n_results` 为 1 到 30 的整数，`llm_enhance` 默认 `false`。成功响应：

```json
{"results":["/media/2f3a2a6d-93f6-4cd0-a4c8-1578c5b929b2"]}
```

结果只包含受控媒体 URL，按相关性和稳定 `meme_id` 排序并去重。语义、向量和索引 generation 均由 PostgreSQL + pgvector 保存；缺少或处于 `pending`/`repair_required` 的记录会跳过，不再使用文件名回退。缓存不存在或正在生成时返回 `503`：

```json
{"error":"cache_not_ready","message":"检索缓存尚未就绪"}
```

旧的 `GET /search?q=...` 不是兼容入口，返回 `405`。

## 图片库和媒体

- `GET /images?search=&page=1&page_size=50`：分页列出当前请求 scope 的扁平图片；每个图片项包含 `meme_id`、文件名、媒体 URL、`metadata.status`、文本索引 `embedding_status` 和图片视觉向量 `visual_embedding_status`。
- `GET /images/metadata?meme_id=`：读取当前 scope 指定 Meme 的完整数据库语境；不接受路径式资源标识。
- `POST /images/rename`：请求 `{ "meme_id": "...", "new_name": "new" }`，保留原扩展名且拒绝覆盖。
- `POST /images/delete`：请求 `{ "meme_id": "..." }`，隔离并删除图片及数据库 Meme。
- `POST /images/upload`：multipart 字段 `auto_name`、多个 `files`，逐文件返回成功或失败，图片直接写入当前请求 scope 的受控图片根；成功入库后自动创建或复用逐图处理 job；不接受目标目录、`scope_id` 或 `user_id`。
- 上传成功会在 PostgreSQL 创建稳定 `meme_id` 和 `pending` 元数据记录；`meme_context.title` 初始为 `null`。数据库是唯一结构化事实，运行时不读取或写入 sidecar。
- `GET /media/{meme_id}`：按当前请求 scope 稳定 Meme ID 受控读取 PNG/JPG/JPEG/GIF；跨 scope ID 返回 `404 meme_not_found`。

- `POST /images/context`：请求 `{ "meme_id": "...", "reverse_image_policy": "forbid|auto" }`，异步创建或复用单图处理 job，返回 `processing_job_id` 和当前 Agent 阶段摘要。缺省策略为 `forbid`。
- `POST /images/context/batch`：请求 `{ "items": [{"meme_id":"..."}], "include_unready": true, "reverse_image_policy": "forbid|auto" }`，逐图返回处理 job 结果；省略 `items` 时不隐式扫描孤立文件。
- `POST /images/visual-embedding` 和 `/images/visual-embedding/batch`：为既有图片提交完整图片处理 Job 的视觉前置；视觉任务失败必须使用完整 Job 重试或受限独立阶段入口。
- `POST /images/metadata/repair`：异步执行数据库记录、图片文件和指纹完整性扫描；不读取 sidecar、不默认调用模型或外部搜索。
- 图片库的“选择图片”“重试选中”和“重试所有未就绪”会调用上述逐图处理接口；有效文本向量写回后会立即具备当前 scope 的搜索资格，不需要为每次上传重建全库缓存。模型切换和存量迁移的显式回填见 [`docs/image-processing-migration.md`](docs/image-processing-migration.md)。

### 图片处理 job

`POST /images/processing?page=1&page_size=100` 按当前 scope 分页枚举图片并提交或复用逐图处理 job，请求体为 `{ "reverse_image_policy": "forbid|auto" }`。单图处理固定为 `visual -> agent -> text_embedding` 三个阶段；每个阶段拥有独立叶子 Task，视觉和文本阶段不消耗 Agent operation grant。活动 job 的配置、目标 SHA、metadata hash 或策略不一致时返回 `409 generation_policy_conflict`。

`GET /images/processing/{job_id}` 返回有限状态：`queued`、`running`、`succeeded`、`failed`、`blocked` 或 `unknown_execution`，以及 `current_stage`、每阶段 `task_id`/attempt/error。跨 scope 或不存在的 ID 都返回 `404 image_processing_job_not_found`。

`GET /images/processing` 返回当前 scope 的完整 Job 父项及 visual、agent、text_embedding 三个阶段；Job 和叶子 Task 的 `submission_mode` 明确为 `pipeline`。

`POST /images/stages` 请求仅接受 `{ "meme_id": "...", "stage": "visual|agent|text_embedding", "reverse_image_policy": "forbid|auto" }`，创建或复用无父 Job 的独立阶段 Task。scope、图片 SHA、配置、grant 和 callback 均由服务端派生；返回 `submission_mode=standalone` 与 `processing_job_id=null`。

`POST /images/processing/{job_id}/retry` 只接受 `failed`、`blocked` 或 `unknown_execution` job，并创建新的 revision、叶子 Task 和必要的 grant；旧 job、Task 和 grant 保持终态。`unknown_execution` 表示外部执行窗口已经开始但结果无法证明，恢复流程不会自动重放，必须由人工确认后显式 retry。兼容路径 `/image-processing/...` 仍可用但不在 OpenAPI 中展示。

## 合集

合集是当前请求 scope 内的逻辑图片分组，使用稳定 `meme_id` 建立成员关系，不复制或移动图片文件。接口不接受 `scope_id` 或 `user_id`。

- `GET /collections?page=1&page_size=50`：按更新时间和合集 ID 稳定分页列出合集，返回 `collection_id`、名称、成员数量、封面媒体 URL 和时间戳。
- `POST /collections`：请求 `{ "name": "工作" }` 创建空合集；名称会去除首尾空白并限制为 1 至 100 个字符。
- `GET /collections/{collection_id}?page=1&page_size=50`：返回合集元数据、成员总数和按加入时间稳定排序的成员。成员包含当前文件名、大小、状态和 `/media/{meme_id}`。
- `PATCH /collections/{collection_id}`：请求 `{ "name": "新名称" }` 重命名，不改变成员关系。
- `DELETE /collections/{collection_id}`：删除合集及成员关系，不删除 Meme 或图片文件。
- `POST /collections/{collection_id}/items`：请求 `{ "meme_ids": ["..."] }` 原子批量加入图片；返回 `added_count`、`existing_count` 和最终 `member_count`。
- `DELETE /collections/{collection_id}/items/{meme_id}`：幂等移除单个成员。

同一 scope 内名称精确唯一，重名返回 `409 collection_exists`；未知合集或图片返回 `404`；非法名称和空成员数组返回 `422`。合集删除或图片删除都会由数据库级联清理关系，但不会影响其他图片。

## 长任务

- `POST /generate-cache`：返回 `202`、`task_id`、`status=queued`；同类任务不会并发执行。
- `GET /tasks?status=running&task_type=meme_context_generation&cursor=...&limit=50`：按状态、类型和 cursor 分页返回安全任务摘要。
- `GET /tasks/{task_id}`：返回任务类型、`queued/running/succeeded/failed`、进度、消息、时间、错误和有限结果。
- `POST /tasks/{task_id}/retry`：图片阶段 Task 一律返回稳定错误 `image_stage_retry_forbidden`；完整 Job 必须调用 Job retry，独立阶段必须调用 `/images/stages`。普通非图片任务仍可使用该入口。

任务记录、去重、claim generation 和租约持久化在 PostgreSQL；服务重启后 queued 可继续执行，过期 running 按租约重新认领或失败。三类图片处理 Task 只由逐图 ImageProcessingWorker 扫描、认领和执行；进程级通用 Worker manager 会排除它们，也不会为图片链创建旧的批次 `cache_generation` finalizer。

## Operation policy

`GET /operations/availability` 或传入单个 `operation` 参数只执行非权威 `probe`，返回可用状态、稳定拒绝原因和可选 `retry_at`，不会创建 reservation 或返回 grant。首期 operation 名称为 `image.upload`、`analysis.agent`、`analysis.reverse_image_search` 和 `image.delete`。适配宿主通过服务端 policy 注入额度和订阅规则；grant 只保存在服务端并绑定 scope、稳定幂等键和真实 Task，客户端字段不能伪造或覆盖它。

图片上传、合集导入、删除和 Agent/反向图片外部副作用分别在 durable 副作用边界执行 acquire/commit；只有能够证明副作用尚未开始时才 release。策略拒绝、额度限制和策略不可用分别映射为 `403 operation_forbidden`、`429 operation_limit_exceeded` 和 `503 operation_policy_unavailable`。

## 内部 Agent callback

`POST /internal/reverse-image/search` 与 `POST /internal/visual-search/match` 不是公共接口。它们在读取 multipart/JSON body 前要求 Runner 注入的短期 HMAC callback token，通过 `X-MemeMeow-Callback`、`X-MemeMeow-Callback-Token` 或 Bearer 头传递；token 绑定当前 Task、scope、claim generation、owner、attempt、目标 SHA、operation、issuer/audience、key id 和过期时间。路由还会从 PostgreSQL 复核 Task 类型、运行状态、租约和目标图片，调用方不能自报 scope、策略或任意图片。发布、轮换、旧任务收束和禁用式回滚见 [`docs/agent-callback-migration.md`](docs/agent-callback-migration.md)。

Agent 只获得当前任务 token、内部地址和 executor token，不获得 callback 根 secret、`SERPAPI_API_KEY` 或数据库凭据。callback secret 由 `MEMEMEOW_AGENT_CALLBACK_SECRET` 配置，生产部署必须通过独立服务身份和网络隔离保护；`MEMEMEOW_AGENT_CALLBACK_VERIFICATION_KEYS` 可用 `kid=secret,kid=secret` 提供轮换期间的旧 key 验证窗口。根 secret 缺失、格式错误或 verifier 异常时 callback 保持不可用，不回退到无认证。凭据轮换后旧 claim 应收束并显式重试。无凭据、旧 claim、租约过期、目标 SHA 变化和超限 body 都以稳定错误拒绝。

## 配置与访问策略

- `GET /config`：只返回模型名、provider 是否配置和 `*_api_key_configured` 布尔状态；完整 URL、路径和密钥不返回。
- 本地视觉活动模型固定为 `dinov2_vitb14`、768 维和 `dinov2_vitb14-rgb224-first-frame-v1`；Compose API 从 `mememeow-visual:8276/health` 读取真实模型状态，权重只在视觉容器内只读挂载并按 `MEMEMEOW_VISUAL_WEIGHTS_SHA256` 校验，未配置时任务返回 `visual_model_not_configured`。历史 DINOv3 向量表保留但不会参与活动匹配。
- 视觉源码目录由 `MEMEMEOW_VISUAL_MODEL_REPO` 配置，仅服务端读取；源码提交和权重许可要求见 [`docs/visual-model-baseline.md`](docs/visual-model-baseline.md)。
- Agent 通过内部 `POST /internal/visual-search/match` 发送 `{ "task_id": "...", "top_k": 20, "exclude_self": true }`。scope、查询图片和向量空间均从运行中的 Agent 任务推导；候选必须同 scope、当前 SHA 有效并有成功 research provenance。
- `.env` 关键字段：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY`、`MEMEMEOW_OPENCODE_MODEL`、`MEMEMEOW_OPENCODE_RUNTIME_ROOT`、`MEMEMEOW_IMAGE_ROOT`、`MEMEMEOW_AGENT_CALLBACK_SECRET`。Compose 首次启动会在 `mememeow-agent-executor-secret` named volume 中生成 0600 的随机 executor token，API 以只读方式读取；旧版 host 运维模式仍可显式设置 `MEMEMEOW_AGENT_EXECUTOR_TOKEN`。生产 API 通过 Compose DNS 调用 executor，不使用 Docker CLI 或 socket。
- `MEMEMEOW_PROTECTED_MODE=true` 时仅放行 `MEMEMEOW_ALLOWED_ENDPOINTS`；限流由 `MEMEMEOW_RATE_LIMIT_*` 控制，超限返回 `429` 和 `Retry-After`。

系统不提供用户登录、注册、JWT、角色或多租户权限接口。资源包和社区同步功能已从生产入口移除。
