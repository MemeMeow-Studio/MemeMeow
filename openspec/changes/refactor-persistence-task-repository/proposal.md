## Why

`backend/database.py` 仍同时承载任务队列、租约 fencing、批次收束、反向图片用量和内部 callback 事实，持久化安全边界难以单独审查。阶段 3 需要把完整任务持久化域集中到 `backend.persistence.repositories`，降低 facade 密度并保持现有 scope、claim/lease、状态、幂等、错误码和分页事实不变。

## What Changes

- 新增 `backend/persistence/repositories/tasks.py`，承载任务提交、scope 读取、批次、公平 claim、lane slot、租约恢复、fenced 写回和稳定分页。
- 新增 `backend/persistence/repositories/reverse_image.py`，承载反向图片 usage 事件和任务级审计摘要。
- 新增 `backend/persistence/repositories/callbacks.py`，承载 PostgreSQL callback request repository、内存 callback 夹具及其完整绑定校验。
- 让 `backend.database` 仅显式 re-export 上述唯一实现，保留历史导入路径和对象身份；`DataEnvironment` 继续使用一个 scope-bound Session 装配全部 repository。
- 增加任务持久化域的 facade、依赖方向、scope、幂等、claim/lease fencing、批次、分页、usage 和 callback 回归契约，并记录验证事实。
- 不修改 ORM schema、migration、HTTP、Worker/运行时、图片文件存储、StorageCoordinator、frontend 或其它 active change。

## Capabilities

### New Capabilities

- `persistence-task-domain`: 任务队列、批次、反向图片审计和内部 callback 事实的 scope-bound 持久化契约。

### Modified Capabilities

无。本 change 只拆分既有实现，不改变公开业务要求。

## Impact

影响公共核心 `backend/database.py`、新增三个 persistence repository 模块、Repository 包入口、资源装配的延迟导入、任务/反向图片/callback 契约测试及本 change artifacts。所有调用方继续使用原 facade 或 `DataEnvironment` 属性；数据库表、约束、migration、任务状态协议和运行时流程保持不变。实现先在 `/home/infstellar/vscode/MemeMeow` 完成并提交，再从本地精确 fetch 单一最终 SHA 到 Server，以一次普通 `--no-ff` merge 引入；不访问 upstream、不 push。
