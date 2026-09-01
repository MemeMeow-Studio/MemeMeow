## Context

公共任务列表目前在 PostgreSQL repository 和兼容内存服务中按 `updated_at` 倒序分页，图片处理父 Job 也按 `updated_at` 倒序读取。Server 工作台将两类数据合并后再次按最近更新时间排序；任务状态和进度写回会改变 `updated_at`。任务和 Job 都已有非空 `created_at`，因此无需新增字段或迁移。

## Goals / Non-Goals

**Goals:**

- 让普通用户工作台以 `created_at DESC, id DESC` 作为唯一列表顺序。
- 让 PostgreSQL cursor 条件、内存兼容分页和前端混排使用相同的不可变创建键。
- 让列表时间列与排序语义一致，同时保留轮询状态和详情字段。
- 保持当前 scope 隔离、筛选、页大小和管理员控制面行为。

**Non-Goals:**

- 不改变 Worker 的 `available_at/created_at/id` 执行选择顺序。
- 不改变管理员跨账户审计列表，它已经按创建时间排序。
- 不新增客户端排序参数、不改变任务状态机、不迁移数据库字段。

## Decisions

### 1. 使用创建时间倒序

采用最新创建任务置顶，延续现有列表的新任务优先方向，同时让任务在进度和状态变化时保持原位置。使用 `id DESC` 作为同一时间戳的确定性次序；数据库 cursor 也使用完全相同的二元组，避免只改 `ORDER BY` 造成分页错位。

### 2. 所有用户列表来源统一创建键

PostgreSQL `TaskRepository.list` 和兼容内存 `PersistentTaskService.list` 均按 `(created_at, task_id)` 倒序处理。图片处理 repository 的父 Job 列表按 `(created_at, id)` 倒序；Worker 恢复扫描改用只读取活动 Job 的 `list_active()`，继续按 `updated_at` 优先，避免工作台展示顺序影响调度。Server 专属管理员查询不改动。

### 3. 前端使用创建时间混排

普通工作台把父 Job 与可见普通任务合并后，以各自 `created_at` 作为时间键，以稳定标识作为并列次序；轮询继续替换最新状态快照，但不会用 `updated_at` 重新排序。列表表头和两类行均使用“创建时间”。

### 4. 兼容历史数据

继续使用现有公开 `created_at` 字段。对兼容内存服务中已经缺少创建时间的历史 JSON 记录，沿用现有反序列化默认值，不增加推测文件时间或重写历史数据的迁移逻辑；工作台仅在缺失时首次用 `updated_at` 作为会话内稳定回退值，正常新建记录始终保存创建时间。

## Risks / Trade-offs

- [用户无法通过列表顺序快速看到最近活动任务] → 状态、进度和活动摘要继续轮询，详情保留完成时间；本次明确优先满足稳定创建顺序。
- [同一时间戳的任务顺序在不同实现中不一致] → 后端和前端均使用 ID 作为明确次级键，并增加相同时间戳测试。
- [旧客户端仍把时间字段理解为最近更新] → API 字段保持兼容但工作台文案改为创建时间，OpenSpec 明确新的列表语义。

## Migration Plan

1. 在开源仓库更新 OpenSpec、任务 repository、内存兼容服务、图片 Job 列表、工作台和测试。
2. 运行任务/API/图片处理/前端定向测试、OpenSpec strict validation、编译检查和 diff check。
3. 提交一个边界清晰的开源 commit，向用户提供 SHA 并暂停在 Server 同步门禁。
4. 用户审核并明确授权后，再从官方 `upstream` 获取精确 SHA，在 Server `main` 上普通 merge；Server 当前重叠 WIP 需先按用户选择保留并处理。

回滚时恢复 `updated_at` 的查询、cursor 和前端显示即可，不涉及数据迁移；创建时间字段和既有状态数据不受影响。
