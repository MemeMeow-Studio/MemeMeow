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
{"results":["/media/a.png"]}
```

结果只包含受控媒体 URL，按相关性和稳定路径排序并去重。缓存只由图片 sidecar 中非空的 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、`meaning` 和 `keywords` 生成，版本为 v4；`search_queries`、`uncertainties` 和 `source_urls` 不参与 embedding。缺少或处于 `pending`/`repair_required` 的 sidecar 会跳过，不再使用文件名回退。缓存不存在或正在生成时返回 `503`：

```json
{"error":"cache_not_ready","message":"检索缓存尚未就绪"}
```

旧的 `GET /search?q=...` 不是兼容入口，返回 `405`。

## 图片库和媒体

- `GET /images?directory=&search=&page=1&page_size=50`：列出图片元数据和一级子目录；每个图片项包含 `metadata.status`（`pending/partial/ready/repair_required`）、`embedding_status`（`pending/ready/blocked`）、可空 `title` 及可安全展示的摘要字段。
- `GET /images/directories?parent=`：列出子目录。
- `POST /images/directories`：请求 `{ "name": "work", "parent": "" }` 创建目录。
- `POST /images/rename`：请求 `{ "directory": "", "filename": "old.png", "new_name": "new" }`，保留原扩展名且拒绝覆盖。
- `POST /images/delete`：请求 `{ "directory": "", "filename": "old.png" }`，同步删除图片和同目录 `old.png.json` sidecar。
- `POST /images/upload`：multipart 字段 `directory`、`auto_name`、多个 `files`，逐文件返回成功或失败。显式启用 `auto_name` 时，系统先保存 Agent 生成的自然语言 `title`，再从标题派生安全文件名；空标题、模型失败或目标冲突时保留原文件名。
- 上传成功会创建同目录的 `图片完整文件名.json` sidecar；`meme_context.title` 初始为 `null`，图片尚未完成语境研究时状态为 `pending`。标题更新本身不会隐式重命名图片。
- `GET /media/{file_path}`：受控读取 PNG/JPG/JPEG/GIF，不接受绝对路径、`..` 或符号链接越界。

- `POST /images/context`：请求 `{ "directory": "", "filename": "a.png" }`，异步创建或复用单图语境 Agent 任务。
- `POST /images/context/batch`：请求 `{ "items": [{"directory":"", "filename":"a.png"}], "include_unready": true }`；省略 items 时扫描既有未就绪图片，逐图返回任务结果。
- `POST /images/metadata/repair`：异步补齐缺失或损坏的 sidecar，并为旧 sidecar 补写 `title: null`；不默认调用模型或外部搜索。
- 图片库的“选择图片”“重试选中”和“重试所有未就绪”会调用上述语境批量接口；语境完成后仍需重新生成检索缓存，图片才会进入 embedding 索引。

## 长任务

- `POST /generate-cache`：返回 `202`、`task_id`、`status=queued`；同类任务不会并发执行。
- `GET /tasks?status=running&task_type=meme_context_generation&cursor=...&limit=50`：按状态、类型和 cursor 分页返回安全任务摘要。
- `GET /tasks/{task_id}`：返回任务类型、`queued/running/succeeded/failed`、进度、消息、时间、错误和有限结果。

任务记录持久化在 `MEMEMEOW_DATA_ROOT/tasks/`；服务重启会保留终态、重新排队 queued，并将遗留 running 标记为 `task_interrupted`。

## 配置与访问策略

- `GET /config`：只返回模型名、Base URL 和 `*_api_key_configured` 布尔状态。
- `.env` 关键字段：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`MEMEMEOW_OPENCODE_EXECUTABLE`、`MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY`、`MEMEMEOW_OPENCODE_MODEL`、`MEMEMEOW_OPENCODE_RUNTIME_ROOT`、`MEMEMEOW_IMAGE_ROOT`。
- OpenCode、skill 和 `.opencode/node_modules` 必须由部署环境预先安装；可用 `MEMEMEOW_OPENCODE_NODE_MODULES` 覆盖该共享依赖目录。启动时会在 `<runtime>/workspace/opencode.json` 写入引用 `MEMEMEOW_OPENCODE_BASE_URL` 与 `MEMEMEOW_OPENCODE_API_KEY` 的 `@ai-sdk/openai` Responses 配置，模型由 `MEMEMEOW_OPENCODE_MODEL` 经命令行传递，并固定使用 `max` 推理强度变体。所有图片 job 共用固定 runtime、DB 和依赖目录，但每张图片使用独立 session。可用 `./scripts/open-opencode.sh` 打开同一 runtime，并通过 `/sessions` 检查历史会话。
- `MEMEMEOW_PROTECTED_MODE=true` 时仅放行 `MEMEMEOW_ALLOWED_ENDPOINTS`；限流由 `MEMEMEOW_RATE_LIMIT_*` 控制，超限返回 `429` 和 `Retry-After`。

系统不提供用户登录、注册、JWT、角色或多租户权限接口。资源包和社区同步功能已从生产入口移除。
