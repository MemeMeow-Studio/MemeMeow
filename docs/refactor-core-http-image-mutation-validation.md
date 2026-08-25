# Image mutation HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-image-mutation-boundary`，用于固定公共实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`0f384299864c9b9937c66d7032857a3cd3073302`
- 变更范围：新增 `backend/image_mutation_http.py`、`tests/test_image_mutation_http.py`、
  本 change artifacts；`api.py` 删除重命名/删除的重复业务编排，只保留 canonical route、
  请求模型和显式宿主 callback 注入。未修改上传、图片处理、图片库只读、MetadataService/
  BlobStore、数据库 schema、scope middleware、Server adapter 或 frontend。

## 验证

- 新增图片变更 callback 模块测试：`12 passed`
- 图片变更/API 定向回归：`13 passed, 36 skipped`
- 图片库/处理/合集/语境/阶段边界回归：`44 passed`
- scope/operation policy 回归：`40 passed`
- 完整非外部门禁：`461 passed, 92 skipped`
- PostgreSQL marker 命令：`553 deselected`，未选择 PostgreSQL 测试且未连接数据库。
- `openspec validate refactor-core-http-image-mutation-boundary --strict`：通过
- `uv run --project "$PWD" --active python -m compileall -q .`：通过
- `git diff --check`：通过

## 对抗性复核

- `/images/rename`、`/images/delete` 各保持单个 `POST` route、`images` tag、原请求模型和旧
  handler 名称；上传与图片库只读 route 未由新模块重复注册。
- 新模块 AST 依赖不包含 `api` 或 `server_api`；metadata service 由入口当前 scope callback
  提供，客户端不能通过模块参数选择其它 scope 或文件路径。
- 重命名继续拒绝路径/控制字符，沿用源扩展名，先校验目标冲突再写 metadata，且只在成功
  后失效检索；metadata target conflict 和其它错误保持稳定投影。
- 删除在 metadata durable 副作用前 acquire `image.delete` grant，明确副作用前错误才
  release；commit 失败不把已完成删除伪装成失败，也不 release 已完成 grant。
- 未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；开源实现和本验证记录提交后，Server 再
按精确 SHA 同步并补记 merge SHA、祖先关系、变更范围和 Server 回归结果。开源仓库未 push。
