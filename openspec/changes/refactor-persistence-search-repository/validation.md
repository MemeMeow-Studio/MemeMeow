# 验证记录

## 基线与范围

- 开源仓库：`/home/infstellar/vscode/MemeMeow`，开始时 `main` 工作区干净；Server 工作区已有 README、OpenCode、Docker、fair-scheduling、PM2 等用户改动，保留且不纳入本 change。
- 原实现：`backend/database.py` 中 `SearchRepository`（基线约 `:131-594`）；本 change 只移动 generation/head、migration state、legacy/incremental text embedding 查询和向量逻辑。
- 保留原路径：`backend.database.SearchRepository` 显式导出 `backend.persistence.repositories.search.SearchRepository` 同一类对象；`DataEnvironment` 仍从兼容 facade 延迟组装共享 Session/scope。
- 明确非范围：VisualEmbeddingRepository、TaskRepository、ReverseImageUsageRepository、AgentCallbackRequestRepository、BlobStore、StorageCoordinator、DatabaseResources、engine/UoW/models、schema/migration、HTTP、frontend 和其它 active change。

## 开源实现范围

- 新增 `backend/persistence/repositories/search.py`，保留原 SearchRepository 方法顺序、SQL、错误码、scope 条件、迁移来源选择、向量过滤与排序语义。
- `backend/database.py` 删除 SearchRepository class 定义并显式 re-export；Repository 包入口增加唯一 canonical export。
- 新增 `tests/test_search_repository_contract.py`，补充单一来源 dispatch、legacy/incremental 排序、limit、维度/零范数错误回归；扩展 `tests/test_persistence_boundaries.py` 覆盖 identity、AST 依赖和周边边界。

## 自动化结果

- 当前目标测试：`17 passed`（`tests/test_persistence_boundaries.py`、`tests/test_database_contract.py`、`tests/test_search_repository_contract.py`）。
- 当前目标模块 `uv run --project "$PWD" --active python -m py_compile ...`：通过。
- 当前 `git diff --check`：通过。
- 本 change `openspec validate refactor-persistence-search-repository --strict`：通过。
- 开源完整回归 `uv run --project "$PWD" --active python -m pytest`：`488 passed, 92 skipped, 3 warnings`。
- PostgreSQL marker `uv run --project "$PWD" --active python -m pytest -m postgres`：`580 deselected`、无选中测试（pytest exit 5）；未配置 `MEMEMEOW_DATABASE_URL`，未连接 PostgreSQL。
- `uv run --project "$PWD" --active python -m compileall -q backend tests alembic`：通过。
- `git diff --check`：通过。
- 全量 `openspec validate --all --strict`：`54 passed, 1 failed`；唯一失败为既有 `support-scope-aware-opencode-workspaces`，本 change 未修改该目录。

## 提交与同步

- 开源实现 SHA：`adc9b9612d2712b226dc0a36de327fb973d15eb2`（`refactor(core): isolate search repository`）。
- 开源验证记录 SHA：待验证记录提交后填入。
- 开源代码收尾 SHA：待收尾提交后填入，作为 Server 精确 fetch 目标。
- Server merge SHA、直接父提交和祖先关系：待精确 fetch/普通 `--no-ff` merge 后填入。
- 开源仓库不访问 `upstream`、不 push；Server 只保留本切片新增变更，不整理其它脏文件。

## 环境门禁与回滚

- 未配置 PostgreSQL 连接串；本次未以 SQLite 冒充 pgvector 验收。
- Compose 未启动，除非当前环境已有显式服务门禁；未运行项必须明确记录。
- 回滚：恢复 `backend/database.py` 原 SearchRepository class，删除新 search 模块、包导出、契约/回归测试和本 change artifacts；不回滚 schema、migration 或业务数据。
