## Why

当前 Agent lane 通过多个 Worker 对具体 `task_id` 竞争全局槽位。虽然数据库租约和 fencing 能防止重复执行，但不同 scope 之间没有可证明的公平顺序，持续提交任务的单一 scope 可能长期占用调度机会。需要把公平选择提升为 PostgreSQL 事务内的公共调度协议，使多进程、重启恢复和租约回收都遵守同一规则。

## What Changes

- 新增跨 scope 的 Agent lane 公平 claim 能力，以持久化公平状态表记录每个 lane/scope 的轮询位置。
- 在一次数据库事务中完成候选 scope 选择、用户级运行额度检查、全局 lane 槽位分配、任务状态变更和公平状态推进。
- 保留现有 `Task.scope_id` 作为可信任务归属；公平调度不得从客户端 payload 推导 scope 或用户身份。
- 支持跳过暂无可执行任务、已达到用户级运行上限或被租约占用的 scope，并在后续槽位释放后继续轮询。
- 保留现有 dedupe、lease、claim generation、fencing、重试和全局背压语义；公平状态损坏时必须 fail-closed，不得退化为无序竞争。
- 为多进程并发 claim、scope 轮询、用户级上限、租约恢复和旧 Worker fencing 增加 PostgreSQL 集成测试。

## Capabilities

### New Capabilities

- `agent-fair-scheduling`: 定义跨 scope 的持久化公平调度、用户级运行上限和原子 claim 协议。

### Modified Capabilities

- `task-status`: 扩展任务认领、Agent lane 并发、背压和恢复要求，使其同时满足公平调度和用户级上限。

## Impact

- 影响 `backend/database.py` 的 Task claim、lane slot 和 migration，`backend/pg_services.py` 的 Worker manager，以及本地任务兼容实现和 PostgreSQL 集成测试。
- 不引入账户、套餐、计费或用户表；公共核心只接受由宿主可信解析出的 opaque scheduling scope，并继续以 `Task.scope_id` 作为任务归属事实。
- 任务 claim 的内部调用协议会增加公平调度入口，但现有 scope-bound `claim(task_id=...)` 兼容路径可保留用于恢复和专用任务。
