# Image processing submission HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-image-processing-submission-boundary`，用于
固定公共实现和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`f8e10799ded1858f367f62635bc28fa9af430c20`
- 变更范围：新增 `backend/image_processing_submission_http.py`、
  `tests/test_image_processing_submission_http.py`、本 change artifacts；`api.py` 删除
  `POST /images/processing` 的重复分页批量编排，只保留 canonical route、query/model 声明和
  显式宿主 callback 注入。未修改 ImageProcessingWorker/Repository、Job 状态机、数据库 schema、
  其它处理路由、scope middleware、Server adapter 或 frontend。

## 验证

- 新增图片处理提交模块测试：`6 passed`
- 图片处理/阶段/context/API 定向回归：`26 passed, 36 skipped`
- 完整非外部门禁：`439 passed, 92 skipped`
- PostgreSQL marker 命令：`55 deselected`，未选择 PostgreSQL 测试且未连接数据库。
- `openspec validate refactor-core-http-image-processing-submission-boundary --strict`：通过
- `uv run --project "$PWD" --active python -m compileall -q api.py backend tests`：通过
- `git diff --check`：通过

## 对抗性复核

- `/images/processing` 保持单个 `POST`、`202`、`images`/`tasks` tags、分页 query 和旧 handler；
  新模块 AST 依赖不包含 `api` 或 `server_api`。
- Worker readiness 在 options/repository/metadata 读取前 fail-closed；客户端只提交既有策略和
  auto-name，Meme、scope、Job 和 attempt 由当前 scope callback 派生。
- 旧 retryable Job 继续传递 `explicit_retry`；同 Job ID 才标记 `reused`；metadata、数据库和
  Worker 单项异常只投影稳定 error 并继续后续图片；响应不包含 payload、路径或 scope。
- 未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；开源实现和验证记录提交后，Server 再按
精确 SHA 同步并补记 merge SHA、祖先关系、变更范围和 Server 回归结果。开源仓库未 push。
