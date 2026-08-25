# Collection import HTTP 重构验证记录

本记录对应 OpenSpec change `refactor-core-http-collection-import-boundary`，用于固定公共实现
和验证事实，供 Server 按精确 SHA 同步。

## 来源

- 实现 commit：`13605361752792926bf2672a7dbda3283bcdf232`
- 安全收束修复 commit：`8b39a0b762c9173feacc0c856ce8e9965cab4e0c`（通过 callback 保留 Server/宿主可收紧的 upload reservation release 错误集合；Server 适配不会因公共模块迁移而放宽）。
- 变更范围：新增 `backend/collection_import_http.py`、`tests/test_collection_import_http.py`、
  本 change proposal/design/spec/tasks；`api.py` 删除 `/collections/import` 重复业务编排，
  保留 canonical route、旧 handler/helper、上传 parser/包 helper 兼容导出和显式宿主 callback。
  未修改 `backend/collection_packages.py`、合集 CRUD、Server 导出、数据库 schema、scope
  middleware、图片处理状态机或 frontend。

## 验证

- 合集导入/合集包/合集 CRUD/API 定向回归：`40 passed`（包含新增导入边界测试 `10 passed`）。
- 上传与 operation policy 兼容回归：`26 passed`。
- 开源完整回归：`475 passed, 92 skipped`。
- PostgreSQL 集成文件：`39 skipped`；当前未设置 `MEMEMEOW_DATABASE_URL`，未连接 PostgreSQL，
  因此本记录不把 skipped 视为数据库验证。`pytest -m postgres` 在当前开源测试配置下为
  `39 deselected`。
- `openspec validate refactor-core-http-collection-import-boundary --strict`：通过。
- `openspec validate --all --strict`：`50 passed, 1 failed`；唯一失败是既有 active change
  `support-scope-aware-opencode-workspaces`，本 change 未修改其 artifacts。
- `uv run --project "$PWD" --active python -m compileall -q .`：通过。
- `git diff --check`：通过。

## 对抗性复核

- `/collections/import`、`/collections/{collection_id}/export` 各保持单个 route，合集 CRUD 未由
  新模块重复注册；新模块不声明 FastAPI route，旧 `api.import_collection` 和
  `_collection_package_error` 仍可导入调用。
- 新模块 AST 依赖不包含 `api` 或 `server_api`；当前 scope environment、metadata BlobStore、
  operation policy、处理 Worker、设置和错误工厂全部由入口 callback 提供，客户端不能选择其它
  scope 或资源路径。
- multipart/ZIP 预检发生在合集创建、文件写入和 operation acquire 之前；资源上限、manifest
  成员 SHA/path、图片身份复用、同名 SHA 后缀和不安全错误投影沿用现有 helper。
- 新成员严格按 acquire → durable upload → commit → 合集关系 → visual/processing task 顺序；
  明确未 durable 的 metadata 错误才 release，commit/任务收束故障不删除或虚假否认已写入事实，
  检索只在存在新 Meme 后失效。
- 逐成员错误不再因缺少 `target_meme_id` 触发未投影异常，失败项保留稳定 error 并在
  `meme_id_map` 使用空字符串；这是对原有部分失败结果意图的 fail-closed 修复。
- 未发现 P1/P2。

## 同步门禁

实现与本验证记录提交后，还需在开源仓库提交收尾 tasks SHA；Server 仅从本地开源历史精确
fetch 收尾 SHA 并普通 `--no-ff` merge，不访问 `upstream`、不 push。
