## 1. 基线与 OpenSpec

- [x] 1.1 记录开源/Server 工作区状态、`pg_services.py` 四类服务的符号边界、调用方和不纳入范围；确认目标路径没有重叠 dirty 改动。
- [x] 1.2 完成 proposal、design、域 spec、任务清单和回滚路径，并通过 OpenSpec 规划校验。

## 2. 应用服务实现边界

- [x] 2.1 新增带中文模块/class/function docstring 的 `backend/services/metadata.py`，逐段迁移 PostgresMetadataService，保持 BlobStore、StorageCoordinator、Meme provenance、scope 和 MetadataError 语义。
- [x] 2.2 新增 `backend/services/search.py`，迁移 PostgresSearchService，保持 embedding/cache、scope 查询、metadata 过滤、排序和错误语义；canonical 模块只依赖 metadata 服务接口。
- [x] 2.3 新增 `backend/services/worker_manager.py`，迁移 PostgresTaskWorkerManager，保持线程池、handler registry、跨 scope recovery、公平 claim、lane slot、resolver 和 shutdown 语义；不导入 TaskService。
- [x] 2.4 新增 `backend/services/tasks.py`，迁移 PostgresTaskService，保持任务提交/去重/批次、claim/lease fencing、错误历史/resume、权限 grant、反向图片审计、分页和 shutdown 语义；只通过 worker manager 回调协作。
- [x] 2.5 将 `backend/pg_services.py` 收敛为兼容 facade，显式 re-export 四个 canonical class，保留旧 `__module__` 导入路径、logger/monkeypatch 兼容，不保留重复 class/helper 实现。
- [x] 2.6 更新必要的 scope factory 类型导入/服务装配注释，确认 API、scripts、image_processing 和 callback 调用方无需迁移；不混入 HTTP、image processing、OpenCode/executor、账户/quota/security 或 schema。

## 3. 契约、回归与对抗性复核

- [x] 3.1 增加 service facade/package identity、唯一实现来源、AST 依赖方向、日志名和旧 import 契约测试。
- [x] 3.2 增加 scope factory、scope payload 拒绝、权限/grant fail-closed、事务和错误/DTO 兼容回归测试。
- [x] 3.3 增加任务状态、dedupe/batch、claim/lease/slot fencing、跨 scope fairness/recovery、resume、审计和分页定向回归。
- [x] 3.4 运行开源目标测试、全量相关回归、OpenSpec strict、compileall、`git diff --check`，记录 PostgreSQL/Compose 实际运行或 skip。
- [x] 3.5 进行一次严格对抗性 review，覆盖安全、权限、事务、并发、迁移、回滚和测试覆盖；修复所有 P1/P2，复审至无未处理高风险项。

## 4. 单一提交、精确同步与 Server 收尾

- [ ] 4.1 在开源仓库将实现、测试、OpenSpec artifacts、validation 和收尾记录合为单一域级提交；固定 SHA、变更范围、验证结果和审核状态，不访问 upstream、不 push。
- [ ] 4.2 用户审核/授权后，从本地精确 fetch 开源收尾 SHA；检查 Server 目标路径无重叠 dirty 改动，以一次普通 `--no-ff` merge 引入并核验祖先关系。
- [ ] 4.3 在同一 Server merge commit 中补入必要的 Server validation、OpenSpec 收束和 `docs/refactor-plan.md` 进度记录，运行 Server 定向/相关全量回归、strict、compileall 和 diff check，记录 PostgreSQL/Compose skip 与残余风险。
