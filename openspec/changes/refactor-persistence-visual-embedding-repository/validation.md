# 验证记录

## 基线与范围

- 开源仓库：`/home/infstellar/vscode/MemeMeow`，开始时 `main` 工作区干净；Server 工作区存在与本任务无关的用户修改，保留且不纳入本 change。
- 原实现：`backend/database.py` 中 `validate_visual_vector` 与 `VisualEmbeddingRepository`（基线约 `:134-301`）；本 change 只移动视觉向量校验、scope/model/SHA 绑定读写和匹配逻辑。
- 保留原路径：`backend.database.VisualEmbeddingRepository` 与 `backend.persistence.repositories.visual_embeddings.VisualEmbeddingRepository` 为同一类；`backend.database.validate_visual_vector` 与 canonical 函数为同一对象；`DataEnvironment` 仍通过兼容 facade 延迟组装共享 Session/scope。
- 明确非范围：Task、ReverseImage、Callback、BlobStore、StorageCoordinator、DatabaseResources、engine/UoW/models、schema/migration、HTTP、视觉推理服务、frontend 和其它 active change。

## 开源实现范围

- 新增 `backend/persistence/repositories/visual_embeddings.py`，保留原方法顺序、SQL、错误码、scope 条件、模型身份、SHA 校验、Agent provenance、活动存储过滤与稳定排序语义。
- `backend/database.py` 删除视觉向量 class/validator 定义并显式 re-export；保留 `MemeVisualEmbedding` 模型历史导出；Repository 包入口增加唯一 canonical export。
- 新增/扩展视觉向量 repository 契约和回归测试，覆盖 identity、依赖方向、输入校验、scope/SHA、候选资格、排除自身与排序。

## 自动化结果

- 开源目标测试：`31 passed`（持久化边界、视觉向量 repository、数据库契约和视觉服务）。
- 开源完整回归：`493 passed, 92 skipped, 3 warnings`。
- PostgreSQL marker：`585 deselected, 0 selected`；未配置 `MEMEMEOW_DATABASE_URL`，未连接 PostgreSQL，也未以 SQLite 冒充 pgvector 验收。
- 本 change `openspec validate refactor-persistence-visual-embedding-repository --strict`：通过。
- `uv run --project "$PWD" --active python -m compileall -q backend tests alembic`：通过。
- `git diff --check`：通过。
- 全量 `openspec validate --all --strict`：`55 passed, 1 failed`；唯一失败为既有 `support-scope-aware-opencode-workspaces`，本 change 未修改该目录。

## 提交与同步

- 开源实现 SHA：`ab9ddda951fdc8973da928e603ccd572dae9f74e`（`refactor(core): isolate visual embedding repository`）。
- 开源验证记录 SHA：`feacc6f6d9401a48bab8f550d32d38571bfb58ff`（`docs: record visual embedding repository validation`）。
- 开源代码收尾 SHA：本次收尾提交；该提交完成后作为 Server 精确 fetch 目标。
- Server merge SHA、直接父提交和祖先关系：待精确 fetch/普通 `--no-ff` merge 后填入。
- 开源仓库不访问 `upstream`、不 push；Server 只保留本切片新增变更，不整理其它脏文件。

## 环境门禁与回滚

- 未配置 PostgreSQL 连接串时，不宣称真实 PostgreSQL/pgvector 验证；Compose 未启动时明确记录未运行。
- 回滚：恢复 `backend/database.py` 原视觉向量校验和 repository class，删除新 repository 模块、包导出、契约/回归测试和本 change artifacts；不回滚 schema、migration 或业务数据。
