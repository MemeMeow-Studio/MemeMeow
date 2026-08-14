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

- `GET /images?search=&page=1&page_size=50`：分页列出当前 local scope 的扁平图片；每个图片项包含 `meme_id`、文件名、媒体 URL、`metadata.status` 和 `embedding_status`。
- `GET /images/metadata?meme_id=`：读取当前 scope 指定 Meme 的完整数据库语境；不接受路径式资源标识。
- `POST /images/rename`：请求 `{ "meme_id": "...", "new_name": "new" }`，保留原扩展名且拒绝覆盖。
- `POST /images/delete`：请求 `{ "meme_id": "..." }`，隔离并删除图片及数据库 Meme。
- `POST /images/upload`：multipart 字段 `auto_name`、多个 `files`，逐文件返回成功或失败，图片直接写入当前 scope 图片根。
- 上传成功会在 PostgreSQL 创建稳定 `meme_id` 和 `pending` 元数据记录；`meme_context.title` 初始为 `null`。数据库是唯一结构化事实，运行时不读取或写入 sidecar。
- `GET /media/{meme_id}`：按当前 scope 稳定 Meme ID 受控读取 PNG/JPG/JPEG/GIF。

- `POST /images/context`：请求 `{ "meme_id": "..." }`，异步创建或复用单图语境 Agent 任务。
- `POST /images/context/batch`：请求 `{ "items": [{"meme_id":"..."}], "include_unready": true }`，逐图返回任务结果；省略 `items` 时不隐式扫描孤立文件。
- `POST /images/metadata/repair`：异步执行数据库记录、图片文件和指纹完整性扫描；不读取 sidecar、不默认调用模型或外部搜索。
- 图片库的“选择图片”“重试选中”和“重试所有未就绪”会调用上述语境批量接口；语境完成后仍需重新生成检索缓存，图片才会进入 embedding 索引。

## 长任务

- `POST /generate-cache`：返回 `202`、`task_id`、`status=queued`；同类任务不会并发执行。
- `GET /tasks?status=running&task_type=meme_context_generation&cursor=...&limit=50`：按状态、类型和 cursor 分页返回安全任务摘要。
- `GET /tasks/{task_id}`：返回任务类型、`queued/running/succeeded/failed`、进度、消息、时间、错误和有限结果。

任务记录、去重、claim generation 和租约持久化在 PostgreSQL；服务重启后 queued 可继续执行，过期 running 按租约重新认领或失败。

## 配置与访问策略

- `GET /config`：只返回模型名、provider 是否配置和 `*_api_key_configured` 布尔状态；完整 URL、路径和密钥不返回。
- `.env` 关键字段：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`MEMEMEOW_OPENCODE_EXECUTABLE`、`MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY`、`MEMEMEOW_OPENCODE_MODEL`、`MEMEMEOW_OPENCODE_RUNTIME_ROOT`、`MEMEMEOW_IMAGE_ROOT`。
- `MEMEMEOW_PROTECTED_MODE=true` 时仅放行 `MEMEMEOW_ALLOWED_ENDPOINTS`；限流由 `MEMEMEOW_RATE_LIMIT_*` 控制，超限返回 `429` 和 `Retry-After`。

系统不提供用户登录、注册、JWT、角色或多租户权限接口。资源包和社区同步功能已从生产入口移除。
