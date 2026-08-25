# PostgreSQL 应用服务 facade 职责域验证记录

本记录对应 OpenSpec change `refactor-service-facades-domain`，聚合 metadata、search、task、
worker-manager 四个应用服务模块、`backend.pg_services` 兼容 facade、契约测试和同步门禁；
不拆分为单 class、单路由或 docs-only change。

## 基线与范围

- 开源仓库基线：`c100a6222f6e2dab8b3d69647a874d233a67156a`，工作区初始 clean。
- Server 基线：`6ac1fd8`；工作区已有 README、opencode、visual Dockerfile、agent tool
  postmortem、public release security、executor、fair scheduling、frontend/PM2 和其它
  active change 脏文件。本 change 目标路径（`backend/pg_services.py`、新增
  `backend/services/`、服务契约测试和本 change 目录）初始没有 Server 重叠修改，未清理或
  覆盖这些文件。
- 范围：新增 `backend/services/{metadata,search,tasks,worker_manager}.py`，让旧
  `backend.pg_services` 只显式 re-export 四个 canonical class；服务模块直接依赖
  `backend.persistence` canonical engine/models/resources/storage，search 单向依赖 metadata，
  tasks 单向依赖 worker-manager，worker-manager 不导入 TaskService。
- 非范围：ORM/schema/migration、Repository SQL、HTTP route/DTO、scope resolver 协议、
  image_processing、OpenCode/executor、反向图片/视觉 provider、账户/quota/security、
  frontend 和其它 active change。

## 自动化验证

- facade/module identity、唯一 class 来源、AST 依赖方向和 logger 契约：`3 passed`（新增
  `tests/test_service_facade_contract.py`）。
- 服务域定向回归（scope、agent concurrency、image processing worker、reverse image、search、
  persistence boundaries）：`50 passed, 16 skipped`。
- 开源完整回归：`504 passed, 92 skipped, 3 warnings`。
- PostgreSQL integration 文件：`39 skipped`；`MEMEMEOW_DATABASE_URL` 和 `DATABASE_URL` 均
  未设置，未实际连接 PostgreSQL/pgvector，不能将 skip 宣称为数据库验证。
- `pytest -m postgres` 未选取本文件测试（`39 deselected`）；项目现有集成测试由环境 fixture
  控制连接，不使用 SQLite/mock 替代 PostgreSQL 证明。
- `openspec validate refactor-service-facades-domain --strict`：通过。
- `openspec validate --changes --strict`：`42 passed, 1 failed`；唯一失败为既有 active change
  `support-scope-aware-opencode-workspaces`，本 change 未修改其 artifacts。
- `uv run python -m compileall -q backend/services backend/pg_services.py`：通过。
- `git diff --check`：通过。
- Docker Compose CLI 可用（v2.28.1），未启动 Compose 服务，未运行真实 Compose identity 或
  PostgreSQL/pgvector E2E。

## 搬迁等价性与对抗性检查

- 四个 class 方法体逐段与拆分前 `backend/pg_services.py` 对照；除模块头、稳定 logger 和
  import 外没有重写 SQL、事务块、状态判断、错误映射或返回 DTO。
- 旧 facade 与 canonical class 对象身份相同；scope factory、API、scripts、图片处理 Worker
  继续通过旧 import 路径取得对象，现有定向和完整回归覆盖了这些兼容入口。
- canonical service 不反向导入 `backend.pg_services`、HTTP 或 Server 模块；worker-manager
  只通过 resolver/handler 协作，任务 scope 仍从持久 Task 事实恢复，不从 payload 推导。
- 对安全、权限/grant fail-closed、事务、并发 claim/lease fencing、恢复、迁移和回滚边界
  完成一次对抗性 review：canonical 模块无 facade/HTTP/Server 反向导入，Worker 不从 payload
  推导 scope，旧 claim/generation/owner/lease 仍由原 repository fencing；未发现 P1/P2，
  无需额外修复。PostgreSQL/Compose 缺失是明确残余风险，不以本地 SQLite 或 mock 代替。

## 提交与同步门禁

- 开源实现、测试、OpenSpec 和主体 validation 已聚合在实现提交
  `bb76073c3d6e807a2c688b574b588d738cb0bad3`；变更范围为四个 canonical service module、
  `backend.pg_services` re-export facade、service identity/依赖契约测试和本 change artifacts。
  随后的同域兼容收束提交保留旧 facade logger 对象身份并完成任务勾选；Server validation
  将记录该最终收束 SHA 及其与主体提交的祖先关系。基线 `c100a6222f6e2dab8b3d69647a874d233a67156a`
  是其祖先；未访问 upstream、未 push。
- 用户任务已明确要求完成本地精确 fetch/普通 merge；Server merge 前仍必须复核目标路径 dirty
  重叠，Server 只允许以一次普通 `--no-ff` merge 引入该精确 SHA。
- Server merge 后需在同一 merge commit 更新本记录、`docs/refactor-plan.md` 和 Server 验证
  结果，记录精确开源/Server SHA、祖先关系、审核状态、变更范围及残余风险。

## 回滚

恢复 `backend/pg_services.py` 的四个原 class 实现并删除 `backend/services/`、契约测试和
本 change artifacts；不回滚 schema、migration、任务数据、文件数据或 policy/grant 事实。
