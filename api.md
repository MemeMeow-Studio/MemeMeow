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

结果只包含受控媒体 URL，按相关性和稳定路径排序并去重。缓存不存在或正在生成时返回 `503`：

```json
{"error":"cache_not_ready","message":"检索缓存尚未就绪"}
```

旧的 `GET /search?q=...` 不是兼容入口，返回 `405`。

## 图片库和媒体

- `GET /images?directory=&search=&page=1&page_size=50`：列出图片元数据和一级子目录。
- `GET /images/directories?parent=`：列出子目录。
- `POST /images/directories`：请求 `{ "name": "work", "parent": "" }` 创建目录。
- `POST /images/rename`：请求 `{ "directory": "", "filename": "old.png", "new_name": "new" }`，保留原扩展名且拒绝覆盖。
- `POST /images/upload`：multipart 字段 `directory`、`auto_name`、多个 `files`，逐文件返回成功或失败。
- `GET /media/{file_path}`：受控读取 PNG/JPG/JPEG/GIF，不接受绝对路径、`..` 或符号链接越界。

## VLM 标注

- `POST /images/describe`：请求 `{ "directory": "", "filename": "a.png" }`，返回 `candidates`，不会修改文件。
- `POST /images/label-batch`：请求 `{ "items": [{"directory":"", "filename":"a.png"}] }`，返回 `202` 和任务标识。

## 长任务

- `POST /generate-cache`：返回 `202`、`task_id`、`status=queued`；同类任务不会并发执行。
- `GET /tasks/{task_id}`：返回任务类型、`queued/running/succeeded/failed`、进度、消息、时间和错误。

任务状态仅保存在进程内存。服务重启后未完成任务视为失败，不会恢复或自动重试。

## 配置与访问策略

- `GET /config`：只返回模型名、Base URL 和 `*_api_key_configured` 布尔状态。
- `.env` 关键字段：`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`VLM_API_KEY`、`VLM_BASE_URL`、`MEMEMEOW_IMAGE_ROOT`。
- `MEMEMEOW_PROTECTED_MODE=true` 时仅放行 `MEMEMEOW_ALLOWED_ENDPOINTS`；限流由 `MEMEMEOW_RATE_LIMIT_*` 控制，超限返回 `429` 和 `Retry-After`。

系统不提供用户登录、注册、JWT、角色或多租户权限接口。资源包和社区同步功能已从生产入口移除。
