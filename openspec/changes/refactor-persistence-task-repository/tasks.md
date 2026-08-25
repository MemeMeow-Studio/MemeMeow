## 1. 基线与 OpenSpec

- [x] 1.1 记录开源/Server 工作区状态、任务持久化域现状、兼容导出和非目标边界。
- [x] 1.2 完成 proposal、任务域 spec、design、回滚路径并通过 OpenSpec 规划校验。

## 2. 任务持久化实现边界

- [x] 2.1 新增带中文模块/class/function docstring 的 `backend/persistence/repositories/tasks.py`，按原顺序迁移 TaskRepository 及 lane、fairness、batch、claim/lease/fenced 写回实现。
- [x] 2.2 新增 `backend/persistence/repositories/reverse_image.py`，迁移 ReverseImageUsageRepository 的 scope、binding、幂等、终态和审计摘要实现。
- [x] 2.3 新增 `backend/persistence/repositories/callbacks.py`，迁移 PostgreSQL callback repository、完整绑定校验、内存双索引夹具和兼容 alias。
- [x] 2.4 修改 `backend/database.py` 删除三组重复实现并显式 re-export canonical 对象；只保留其它模型、Repository、BlobStore、StorageCoordinator 和运行时事实。
- [x] 2.5 更新 Repository 包入口与 `DataEnvironment` lazy assembly，确保所有对象共享同一 scope-bound Session，canonical 模块不顶层导入 `backend.database`。

## 3. 契约、回归与文档

- [x] 3.1 增加 facade/package identity、唯一实现来源、依赖方向和周边边界契约测试。
- [x] 3.2 增加任务 scope、dedupe、claim/lease、fairness、slot fencing、batch、错误码和稳定 cursor 分页回归测试。
- [x] 3.3 增加 usage/callback 双索引、改绑拒绝、终态幂等、schema fail-closed 和内存夹具回归测试。
- [x] 3.4 运行开源目标测试、PostgreSQL marker、完整回归、OpenSpec strict、compileall、`git diff --check`，新增 validation 记录。

## 4. 单一提交、精确同步与 Server 收尾

- [x] 4.1 将实现、测试、OpenSpec artifacts、validation 和开源收尾合并为一个开源最终提交，不访问 upstream、不 push。
- [ ] 4.2 检查 Server 目标路径无重叠脏改动，从本地精确 fetch 开源最终 SHA，以一次普通 `--no-ff` merge 引入并核验祖先关系。
- [ ] 4.3 在同一 Server merge commit 中补入必要的 validation、OpenSpec 收束和 `docs/refactor-plan.md` 记录，运行 Server 目标/完整回归、静态门禁并记录 PostgreSQL/Compose skip。
