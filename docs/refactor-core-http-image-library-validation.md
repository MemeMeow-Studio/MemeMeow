# Image library HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-image-library-boundary`，用于固定公共实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`251d377e3e47a33b455dc83c22d8e717f95018a9`
- 变更范围：新增 `backend/image_library_http.py`、`tests/test_image_library_http.py`、
  本 change artifacts；`api.py` 删除图片列表、metadata、media 的重复只读编排，只保留
  canonical route、query 声明和显式宿主 callback 注入。未修改上传/重命名/删除、图片处理
  Worker、MetadataService/BlobStore、数据库 schema、scope middleware、Server adapter 或 frontend。

## 验证

- 新增图片库 callback 模块测试：`9 passed`
- 图片库/API/数据库契约定向回归：`16 passed, 36 skipped`
- 完整非外部门禁：`433 passed, 92 skipped`
- PostgreSQL marker 命令：`55 deselected`，未选择 PostgreSQL 测试且未连接数据库。
- `openspec validate refactor-core-http-image-library-boundary --strict`：通过
- `uv run --project "$PWD" --active python -m compileall -q api.py backend tests`：通过
- `git diff --check`：通过

## 对抗性复核

- `/images`、`/images/metadata`、`/media/{meme_id}` 各保持单个 `GET` route、`images` tag、
  原 query/path 参数和旧 handler 名称；新模块 AST 依赖不包含 `api` 或 `server_api`。
- 图片列表拒绝目录、scope、user 和未知 query selector；所有 Meme、metadata、embedding、
  visual 和 processing 状态由当前 scope service/environment 派生，无法证明文件指纹的记录
  不进入公开列表。
- metadata/media 只接受稳定 `meme_id`，`image_for_meme` 继续在返回 sidecar 或 FileResponse
  前校验 BlobStore 路径及数据库 SHA/size；不存在/错配错误只投影稳定 code/status，不泄露物理路径。
- 未发现 P1/P2。

## 同步门禁

用户已批准从本地开源仓库精确 fetch 并普通 merge；开源实现和验证记录提交后，Server 再按
精确 SHA 同步并补记 merge SHA、祖先关系、变更范围和 Server 回归结果。开源仓库未 push。
