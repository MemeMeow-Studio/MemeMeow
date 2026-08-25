# 验证记录

## 代码提交

- 实现提交：`dae492ab5f33672d98614fc675bf3766f0cbe86f`
- 兼容 facade/契约收束提交：`e955865ecf9931b82e87a3bc0f2164719c488f40`
- 验证记录提交：以本文件所在提交为准，收尾提交会补充精确 SHA。
- 收尾提交：Server 同步授权链完成后补记开源收尾 SHA 与 Server merge SHA。

## 自动化结果

- 持久化 engine/UoW/resources 目标契约：`22 passed, 1 warning`。
- 相关 scope/API/反向图片服务回归：`22 passed, 52 skipped`。
- 相关持久化、公平调度、策略、callback、合集存储回归：`47 passed, 1 warning`。
- 开源完整回归：`483 passed, 92 skipped`，3 个既有 warning，无失败。
- PostgreSQL/API/反向图片 skip：未设置 `MEMEMEOW_TEST_DATABASE_URL`，未连接 PostgreSQL；
  PostgreSQL 集成、API PostgreSQL 和反向图片 PostgreSQL 门禁均因此跳过。
- Compose runtime identity：未显式设置 `MEMEMEOW_RUNTIME_IDENTITY_E2E=1`，未启动 Compose
  身份验收。
- 本 change OpenSpec strict validate：通过。
- 全量 OpenSpec strict validate：`52 passed, 1 failed`；唯一失败为既有 active change
  `support-scope-aware-opencode-workspaces` 的公平调度场景完整性，本 change 未修改该目录。
- `uv run python -m compileall -q backend tests alembic`：通过。
- `git diff --check`：通过。

## 复核与回滚

对新模块的导入循环、facade 对象身份、事务提交/回滚/关闭、scope 绑定、local/non-local
storage namespace、optional control schema 委托、flat preflight 委托和变更范围进行了
对抗性复核；engine 六个函数体与旧实现逐项比对一致，未发现未处理的 P1/P2 风险。未修改
schema/migration、Repository、BlobStore、StorageCoordinator、HTTP 或任务协议。

回滚顺序：先恢复 `backend/database.py` 中被 facade 替换的 engine/UoW/resources 实现，
再删除 `backend/persistence/engine.py`、`unit_of_work.py`、`resources.py`、契约测试和本
change artifacts；不回滚任何数据库 revision 或数据。
