# 验证记录

## 基线与范围

- 开源仓库：`/home/infstellar/vscode/MemeMeow` 开始时 `main` 工作区干净；Server 工作区已有与本切片无关的修改，后续同步保留且不纳入提交。
- 本切片将 `TaskRepository`、`ReverseImageUsageRepository`、`AgentCallbackRequestRepository` 和内存 callback 夹具分别移动到 `backend/persistence/repositories/{tasks,reverse_image,callbacks}.py`。
- `backend.database` 保留旧导出对象身份；`DataEnvironment` 继续通过 lazy facade 以同一个 UnitOfWork Session 装配 scope-bound repository。
- 未移动 ORM models、schema/migration、Meme/Collection/Search/VisualEmbedding Repository、BlobStore、StorageCoordinator、HTTP、Worker、provider、callback token 验证或 frontend。

## 开源实现与自动化结果

- facade/package identity、canonical 单一实现来源、顶层依赖方向和周边边界测试已补充；任务/usage/callback 既有回归继续复用同一实现。
- 定向持久化、数据库模型、任务 HTTP、反向图片 HTTP、视觉 callback、callback 夹具和并发配置测试：`84 passed`。
- 完整开源回归：`494 passed, 92 skipped, 3 warnings`。
- PostgreSQL marker：`39 deselected`（命令因无选中测试返回 pytest code 5）；未配置 `MEMEMEOW_DATABASE_URL`，未连接 PostgreSQL，也未用 SQLite 冒充 advisory lock、复合唯一约束或 callback schema 验收。
- 本 change `openspec validate refactor-persistence-task-repository --strict`：通过。
- 全量 `openspec validate --all --strict`：`56 passed, 1 failed`；唯一失败为既有 `support-scope-aware-opencode-workspaces`，本 change 未修改该目录。
- `uv run --active python -m compileall -q backend tests alembic`：通过。
- `git diff --check`：通过。

## 提交与同步

- 按新的单提交门禁，以下实现、测试、OpenSpec artifacts、validation 和收尾内容在同一个开源最终提交中固定；该 SHA 是 Server 唯一 fetch/merge 目标。
- 开源最终 SHA：待提交后填入。
- Server merge SHA、双父提交和祖先关系：待本地精确 fetch/普通 `--no-ff` merge 后填入。
- 开源不访问 `upstream`、不 push；Server 不整理、不暂存、不删除其它脏文件。

## 环境门禁与回滚

- Compose CLI 可用但未启动服务；未运行 Compose identity 或真实 PostgreSQL/pgvector E2E，Server 收尾时继续明确记录该事实。
- 回滚：恢复 `backend/database.py` 原任务/usage/callback 实现并删除新 repository 模块、包导出、契约测试和 change artifacts；不回滚 schema、migration 或业务数据。
