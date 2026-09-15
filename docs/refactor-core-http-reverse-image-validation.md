# Reverse image HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-reverse-image-boundary`，用于固定公共实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`082e454884f2a736d9f8f4988b649b41298010de`
- 变更范围：新增 `backend/reverse_image_http.py`、`tests/test_reverse_image_http.py`、
  本 change artifacts；`api.py` 删除反向图片 callback 的重复编排，只保留 canonical route、
  multipart 表单声明和显式宿主 callback 注入。未修改 ReverseImageService、缓存/provider、
  callback token middleware、数据库 repository/schema、scope middleware、Server adapter 或 frontend。

## 验证

- 新增反向图片 callback 模块测试：`11 passed`
- callback/反向图片/scope/API/安全定向回归：`62 passed`
- 完整非外部门禁：`423 passed, 92 skipped`
- PostgreSQL 集成命令：未设置 `MEMEMEOW_TEST_DATABASE_URL`，测试显式 skip，未连接默认数据库。
- PostgreSQL marker 命令：当前开源仓库未选择 PostgreSQL 测试，结果为 deselected，未连接数据库。
- `openspec validate refactor-core-http-reverse-image-boundary --strict`：通过
- `uv run --project "$PWD" --active python -m compileall -q api.py backend tests`：通过
- `git diff --check`：通过

## 对抗性复核

- `/internal/reverse-image/search` 保持单个 `POST`、`internal` tag、route 名称和旧 handler import；
- multipart body 上限、binding/registration、task scope/claim/attempt、request id/header/digest 在 reverse-image service 前 fail-closed；上传图片可以是任意有效图片，不再要求与任务原图 SHA 相同；
- 每个 `meme_context_generation` Task 的 Agent 语境最多一次反向图片 provider 调用。相同逻辑请求可恢复已有事实，不同图片、参数、refresh 或 request ID 返回 `reverse_image_call_limit_reached`；provider 失败或未知执行也不得重放。
- `ReverseImageError` 与 `DatabaseError` 只投影稳定 status/code/message，不泄露 provider 或
  数据库内部正文。未发现 P1/P2。

## 同步门禁

本次公共核心改动尚未创建 commit，未 push，也未同步 Server。必须先在开源仓库完成审核并获得用户明确授权，之后才能按精确 SHA 同步。
