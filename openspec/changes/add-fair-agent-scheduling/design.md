## Context

当前 `Task` 持久化 `scope_id`、lane、状态、租约和 claim generation；`TaskLaneSlot` 以 `(lane, slot_number)` 表示共享全局执行槽位。Worker 先扫描 queued task，再按指定 `task_id` 竞争 slot，因此已有互斥和 fencing，但没有跨 scope 的公平选择。公共核心没有账户实体，公平分组只能使用可信的 scope 归属或宿主提供的 opaque scheduling key。

## Goals / Non-Goals

**Goals:**

- 在 PostgreSQL 中持久化每个 lane/scope 的公平状态。
- 用事务内的最久未服务轮询实现可测试的跨 scope 公平。
- 在同一 claim 事务内 enforce 全局 slot 和 scope 运行上限。
- 保留现有 dedupe、lease、claim generation、fencing、恢复和错误语义。
- 允许多个 API/Worker 进程共享同一公平顺序。

**Non-Goals:**

- 不在公共核心引入账户、订阅、套餐或计费模型。
- 第一版不实现加权公平、优先级队列、抢占式取消或跨 lane 全局公平。
- 不改变任务状态 API 对 scope 的隔离，也不向客户端暴露公平状态。

## Decisions

### 1. 增加独立公平状态表

新增 `task_lane_fairness` 表，以 `(lane, scope_id)` 为唯一键，保存 `last_dispatch_sequence` 和更新时间。每个成功 claim 在 lane advisory lock 内生成下一个单调序号并更新所选 scope。选择 `last_dispatch_sequence` 最小的可执行 scope，初次出现的 scope 使用稳定创建顺序和 scope ID 打破平局。

选择独立表而不是把字段塞进 `Task`，因为公平状态是 scope/lane 聚合事实，不属于某一个任务；任务完成、失败和去重不会覆盖该状态。

### 2. 新增全局公平 claim 入口，保留 scope-bound 读取

Worker manager 使用新的跨 scope `claim_next` 入口；该入口先读取候选 scope，再返回带有 `scope_id` 的 Task，随后按该 scope 创建 service facade。现有 scope-bound `claim(task_id=...)` 保留给兼容恢复、专用任务和已完成公平选择后的内部收束，但 Agent 正常调度不得再依赖无序扫描竞争。

### 3. 以 scope 作为公共公平分组

公共核心将 scope 作为可信公平分组，不解析客户端 `user_id`。Server 负责把认证账户映射到 scope；未来需要一个用户多个 scope 时，再由宿主注入稳定 opaque scheduling key，不改变任务 payload 的信任边界。

### 4. 选择与占槽必须一个事务完成

公平 claim 的事务顺序固定为：lane advisory lock -> 清理/识别过期 slot -> 读取每个可执行 scope 的最早候选任务 -> 选择最久未服务 scope -> `FOR UPDATE SKIP LOCKED` 锁定任务 -> 分配 lane slot -> 更新 Task claim/lease -> 更新公平序号 -> commit。任何一步失败都回滚，公平状态不会在任务未成功进入 running 时前进。

### 5. 用“最久未服务”实现第一版轮询

不保存一个只在内存中的 cursor，也不依赖调度线程提交顺序。最久未服务序号等价于 round-robin：只要 scope 在连续选择时都可执行，它们会交替获得成功 claim；达到 scope 上限、没有 queued task 或行被锁定时允许跳过。这样既能处理 scope 动态加入，也能在多进程下保持确定性。

### 6. 公平状态故障必须 fail-closed

公平状态表缺失、重复、不可读或更新失败时，Agent lane 返回稳定调度不可用错误，不自动回退到旧竞争式 claim。已有 running task 继续由 lease/fencing 管理，避免公平协议失效时产生隐性超配或不公平绕过。

## Risks / Trade-offs

- [公平状态表成为新的事务热点] -> 只在 lane advisory lock 持有期间更新小行，候选查询限制每个 scope 一条最早任务，并增加索引和 claim 延迟指标。
- [严格轮询降低单用户空闲时的利用率] -> 无可执行 scope 时允许跳过；全局 slot 仍按可执行候选填满，不为不存在的用户预留槽位。
- [scope 数量很多时候选查询成本上升] -> 维护每个 scope 的公平行和 queued 计数索引，第一版限制每次调度扫描范围并用稳定分页；不在内存复制完整 scope 列表。
- [旧调用路径绕过公平协议] -> Agent lane 的正常 manager claim 强制走 `claim_next`，兼容接口只接受内部已选任务，不作为公网可调用能力。
- [迁移期间公平状态为空] -> migration 后按当前 queued/running 任务惰性补齐公平行，初次顺序由 `created_at`、scope ID 稳定决定，不改变已有 Task 状态。

## Migration Plan

1. 在开源仓库先增加表、claim 协议、公共 spec 和 PostgreSQL 集成测试。
2. migration 创建公平状态表和索引；旧任务不回写历史公平序号，首次调度按稳定初始顺序建立状态。
3. 部署时先让 Worker 支持读写公平状态，再启用 Agent lane 的公平 claim；不支持新协议的旧 Worker 必须停止，避免两种 claim 语义并存。
4. Server 在公共 commit 审核并同步后增加账户/scope 映射和配置。回滚时先停止新 Worker，再恢复旧 Worker；由于公平状态只影响后续 claim，不修改已完成任务，但回滚后的竞争式调度不再提供公平保证。
