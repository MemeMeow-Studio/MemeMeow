# MemeMeow API

FastAPI 是唯一业务入口。模型密钥、Base URL 和路径只从服务端 `.env` 读取，前端不能修改配置或获得绝对路径。

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

- `GET /images?search=&page=1&page_size=50`：分页列出当前 local scope 的扁平图片；每个图片项包含 `meme_id`、文件名、媒体 URL、`metadata.status`、文本索引 `embedding_status` 和图片视觉向量 `visual_embedding_status`。
- `GET /images/metadata?meme_id=`：读取当前 scope 指定 Meme 的完整数据库语境；不接受路径式资源标识。
- `POST /images/rename`：请求 `{ "meme_id": "...", "new_name": "new" }`，保留原扩展名且拒绝覆盖。
- `POST /images/delete`：请求 `{ "meme_id": "..." }`，隔离并删除图片及数据库 Meme。
- `POST /images/upload`：multipart 字段 `auto_name`、多个 `files`，逐文件返回成功或失败，图片直接写入当前 scope 图片根。
- 上传成功会在 PostgreSQL 创建稳定 `meme_id` 和 `pending` 元数据记录；`meme_context.title` 初始为 `null`。数据库是唯一结构化事实，运行时不读取或写入 sidecar。
- `GET /media/{meme_id}`：按当前 scope 稳定 Meme ID 受控读取 PNG/JPG/JPEG/GIF。

- `POST /images/context`：请求 `{ "meme_id": "..." }`，异步创建或复用单图语境 Agent 任务。
- `POST /images/context/batch`：请求 `{ "items": [{"meme_id":"..."}], "include_unready": true }`，逐图返回任务结果；省略 `items` 时不隐式扫描孤立文件。
- `POST /images/visual-embedding` 和 `/images/visual-embedding/batch`：为既有图片显式回填当前视觉模型向量；视觉任务失败可用 `POST /tasks/{task_id}/retry` 只重试视觉阶段。
- `POST /images/metadata/repair`：异步执行数据库记录、图片文件和指纹完整性扫描；不读取 sidecar、不默认调用模型或外部搜索。
- 图片库的“选择图片”“重试选中”和“重试所有未就绪”会调用上述语境批量接口；语境完成后仍需重新生成检索缓存，图片才会进入 embedding 索引。

## 合集

合集是当前 `local` scope 内的逻辑图片分组，使用稳定 `meme_id` 建立成员关系，不复制或移动图片文件。接口不接受 `scope_id` 或 `user_id`。

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
- `POST /tasks/{task_id}/retry`：只重试所选失败阶段；视觉成功不会重跑 Agent，Agent 重试不会重跑视觉，文本索引重试不会级联前两阶段。

任务记录、去重、claim generation 和租约持久化在 PostgreSQL；服务重启后 queued 可继续执行，过期 running 按租约重新认领或失败。

## 配置与访问策略

- `GET /config`：只返回模型名、provider 是否配置和 `*_api_key_configured` 布尔状态；完整 URL、路径和密钥不返回。
- 本地视觉活动模型固定为 `dinov2_vitb14`、768 维和 `dinov2_vitb14-rgb224-first-frame-v1`；Compose API 从 `mememeow-visual:8276/health` 读取真实模型状态，权重只在视觉容器内只读挂载并按 `MEMEMEOW_VISUAL_WEIGHTS_SHA256` 校验，未配置时任务返回 `visual_model_not_configured`。历史 DINOv3 向量表保留但不会参与活动匹配。
- 视觉源码目录由 `MEMEMEOW_VISUAL_MODEL_REPO` 配置，仅服务端读取；源码提交和权重许可要求见 [`docs/visual-model-baseline.md`](docs/visual-model-baseline.md)。
- Agent 通过内部 `POST /internal/visual-search/match` 发送 `{ "task_id": "...", "top_k": 20, "exclude_self": true }`。scope、查询图片和向量空间均从运行中的 Agent 任务推导；候选必须同 scope、当前 SHA 有效并有成功 research provenance。
- `.env` 关键字段：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY`、`MEMEMEOW_OPENCODE_MODEL`、`MEMEMEOW_OPENCODE_RUNTIME_ROOT`、`MEMEMEOW_IMAGE_ROOT`。Compose 首次启动会在 `mememeow-agent-executor-secret` named volume 中生成 0600 的随机 executor token，API 以只读方式读取；旧版 host 运维模式仍可显式设置 `MEMEMEOW_AGENT_EXECUTOR_TOKEN`。生产 API 通过 Compose DNS 调用 executor，不使用 Docker CLI 或 socket。
- `MEMEMEOW_PROTECTED_MODE=true` 时仅放行 `MEMEMEOW_ALLOWED_ENDPOINTS`；限流由 `MEMEMEOW_RATE_LIMIT_*` 控制，超限返回 `429` 和 `Retry-After`。

系统不提供用户登录、注册、JWT、角色或多租户权限接口。资源包和社区同步功能已从生产入口移除。
