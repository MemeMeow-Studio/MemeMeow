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
  新模块 AST 依赖不包含 `api` 或 `server_api`。
- multipart body 上限、binding/registration、task scope/claim/attempt、目标 Meme SHA 和
  request id/header/digest 均在 reverse-image service 前 fail-closed；客户端没有 scope/path/target
  选择字段。
- `auto_crop` 只在上传整图 SHA 与持久目标匹配后调用确定性服务端裁剪；service 收到的请求
  保留 callback binding、目标 SHA 和规范化请求绑定。
- `ReverseImageError` 与 `DatabaseError` 只投影稳定 status/code/message，不泄露 provider 或
  数据库内部正文。未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；开源实现和验证记录提交后，Server 再按
精确 SHA 同步并补记 merge SHA、祖先关系、变更范围和 Server 回归结果。开源仓库未 push。
