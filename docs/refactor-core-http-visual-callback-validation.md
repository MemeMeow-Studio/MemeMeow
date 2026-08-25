# Visual callback HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-visual-callback-boundary`，用于固定公共实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`7c1b68d2e1dde6d129e89e6f528656c4baf8c1ff`
- 实现父提交：`39d182d31a8b2f9ea59a1789bdd874f3632498cc`
- 变更范围：新增 `backend/visual_callback_http.py`、`tests/test_visual_callback_http.py`、
  本 change artifacts；`api.py` 删除视觉匹配 callback 的重复模型/编排，只保留 canonical
  route、legacy model/handler 和显式宿主 callback 注入。未修改 callback token 生成、数据库
  repository/schema、VisualSearchService、scope middleware、Server adapter 或 frontend。

## 验证

- 新增视觉 callback 模块测试：`14 passed`
- callback/visual/scope/API 定向回归：`70 passed, 36 skipped`
- 完整非外部门禁：`413 passed, 92 skipped`
- PostgreSQL 集成命令：`uv run pytest tests/test_postgres_integration.py tests/test_reverse_image_service.py -q`，
  未设置 `MEMEMEOW_TEST_DATABASE_URL`，结果为 `55 skipped`，未连接默认数据库。
- PostgreSQL marker 命令：`uv run pytest tests/test_postgres_integration.py tests/test_reverse_image_service.py -m postgres -q`；
  当前开源仓库未注册 `postgres` marker，结果为 `55 deselected`（无测试被选择，pytest exit code 5），未连接数据库。
- `openspec validate refactor-core-http-visual-callback-boundary --strict`：通过
- `uv run python -m compileall -q .`：通过
- `git diff --check`：通过

## 对抗性复核

- `/internal/visual-search/match` 保持单个 `POST`、`internal` tag、route 名称和旧 model/handler
  import；新模块 AST 依赖不包含 `api` 或 `server_api`。
- 请求模型继续 `extra="forbid"`，客户端不能提交 scope、path 或其它执行事实；注入 binding
  还必须是真实 `CallbackBinding`，header/body request id 均按既有格式校验。
- 缺失 binding、缺失 registration、task id 不匹配、旧 claim 和 stale attempt 在视觉 service
  前 fail-closed；scope 由 binding 派生，task、claim generation、attempt、target SHA 和
  registration operation 由持久事实复核。
- callback fact 使用完整 input digest；completed 对象直接 replay；started fact 先提交再调用
  service；`VisualSearchError`/`DatabaseError` 均写入 failed fact 后投影稳定 status/code；成功
  结果写入 completed fact。未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；Server 尚未同步本 change，待同步后记录
Server merge SHA、祖先关系、变更范围和 Server 回归结果。开源仓库未 push。
