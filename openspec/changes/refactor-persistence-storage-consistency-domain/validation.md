# 文件一致性与持久化存储协调域验证记录

本记录对应 OpenSpec change `refactor-persistence-storage-consistency-domain`，聚合
BlobStore、StorageCoordinator、资源装配、旧 facade、契约测试和失败/恢复验证。实现、
测试、OpenSpec 和本记录保持同一职责域提交链，不拆分为单 Repository、单路由或 docs-only
change。

## 基线与范围

- 开源仓库基线：`0f02b6c`（task persistence domain 已完成，工作区初始 clean）。
- Server 初始工作区存在与本域无关的用户/active-change 脏文件，包括 README、opencode、
  visual Dockerfile、agent tool postmortem、public release security、executor、fair
  scheduling、frontend/PM2 等；`backend/database.py`、`backend/persistence/resources.py`、
  storage 测试和本 change 目录初始无重叠脏改动，未触碰这些文件。
- 范围：新增 `backend/persistence/storage.py` 作为 BlobStore/StorageCoordinator 唯一实现，
  resources 直接装配 canonical storage，`backend.database` 显式兼容导出；不修改 schema、
  migration、HTTP、frontend 或 Server 专属控制面。
- 已完成 persistence changes：models、engine/resources、meme/collection、search、
  visual embedding、task repository；没有其它未完成 storage 相关 change，本域使用单一
  聚合 change。

## 自动化验证

- Storage canonical/facade、路径/符号链接、暂存/隔离、状态机和 storage schema 契约：
  `21 passed`（含新增 `tests/test_storage_consistency_contract.py`）。
- 相关 API/图片变更/处理/反向图片/视觉/搜索/scope 回归：`69 passed, 52 skipped`。
- PostgreSQL 集成文件：`39 skipped`；`MEMEMEOW_DATABASE_URL` 未设置，未实际连接
  PostgreSQL/pgvector，不能把 skip 宣称为真实数据库验证。
- 开源完整回归：`501 passed, 92 skipped, 3 warnings`。
- 本 change：`openspec validate refactor-persistence-storage-consistency-domain --strict`
  通过。
- 全量 OpenSpec strict：`57 passed, 1 failed`；唯一失败是既有 active change
  `support-scope-aware-opencode-workspaces`，本 change 未修改其 artifacts。
- `uv run python -m compileall -q backend api.py executor`：通过。
- `git diff --check`：通过。
- Docker Compose CLI 可用（v2.28.1），未启动 Compose 服务；未运行真实 Compose identity
  或 PostgreSQL/pgvector E2E。

## 对抗性复核

- canonical 类对象与旧 facade 身份相同；storage 模块无 `backend.database`、HTTP 或
  Server 适配导入，database.py 不再声明 BlobStore/StorageCoordinator。
- BlobStore 保留受控根目录、scope namespace、内部 staging/quarantine 隔离、独占写入、
  fsync、符号链接/穿越拒绝、原子不覆盖移动和 SHA/size 复核。
- StorageCoordinator 保留 upload/rename/delete 的 durable operation、合法状态转移、
  CAS revision/claim/owner/attempt/title fencing、恢复器 `SKIP LOCKED`、补偿与
  ambiguous/blocked/unknown_execution fail-closed 语义；不会把 durable/unknown 事实当作
  可安全 release 或成功。
- 未发现 P1/P2；完成复审后未改变现有 schema、migration、公开错误投影或 operation/grant
  计费规则。未配置 PostgreSQL 的残余风险已明确记录，不以 SQLite/mocks 代替数据库证明。

## 提交与同步门禁

- 开源实现/测试/OpenSpec 聚合提交 SHA：待本次聚合提交后填写。
- 开源验证/收尾 SHA：与上述聚合提交相同；不创建 docs-only 收尾提交。
- Server merge commit SHA：待用户授权后的本地精确 fetch/普通 `--no-ff` merge 后填写。
- 祖先关系：Server merge 完成后必须证明 `git merge-base --is-ancestor <open-close-sha> <server-head>`。
- 审核/授权：本任务已明确授权本地精确 fetch/普通 merge；不访问 upstream、不 push。
- Server 侧验证：merge 后补记定向回归、strict validate、compileall、diff check、真实
  PostgreSQL/Compose skip 事实及残余风险。

## 回滚

先恢复 `backend/database.py` 的 facade/实现对应关系，再删除 canonical storage、契约测试和
本 change artifacts；不回滚 schema、migration 或数据库数据。任何无法证明文件副作用的
operation 保留 `blocked` durable fact，交给恢复/人工处置。
