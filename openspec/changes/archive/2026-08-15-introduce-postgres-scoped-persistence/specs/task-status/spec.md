## MODIFIED Requirements

### Requirement: 系统必须提供统一任务状态
任务状态接口 MUST 只返回当前 scope 的任务，并 MUST 返回任务类型、状态、进度（无法估计时可为 `null`）、当前消息、创建时间、完成时间和错误信息（如有）。任务状态 MUST 持久化到 PostgreSQL；任务只能从 `queued` 进入 `running`，再进入 `succeeded` 或 `failed`，租约到期后的重新认领不得被报告为第二个任务。

#### Scenario: 查询运行中任务
- **WHEN** 客户端查询当前 scope 中存在的运行中任务
- **THEN** 系统返回 `running` 状态及最新可用进度，不重复创建或执行第二份任务记录

#### Scenario: 查询未知任务
- **WHEN** 客户端查询不存在或不属于当前 scope 的 `task_id`
- **THEN** 系统返回 `404`，错误标识为 `task_not_found`，不泄露任务归属

### Requirement: 服务重启必须使未完成内存任务失败
任务事实 MUST 持久化到 PostgreSQL，而不是只存在于进程内存。服务或 Worker 重启后，`queued` 任务 MUST 保持可执行；失去有效执行租约的 `running` 任务 MUST 按重试策略被重新认领或进入可诊断的 `failed` 终态，系统不得永久报告一个无人执行的运行中任务。

#### Scenario: 服务在排队期间重启
- **WHEN** 服务重启且任务仍为 `queued`
- **THEN** 新 Worker 从 PostgreSQL 认领并继续执行同一 `task_id`

#### Scenario: 服务在任务执行期间重启
- **WHEN** 执行 `running` 任务的 Worker 退出且租约过期
- **THEN** 其他 Worker 重新认领同一任务，或在达到重试上限后将其标记为 `failed` 并记录稳定错误

## ADDED Requirements

### Requirement: 任务认领和去重必须支持并发进程
系统 MUST 使用数据库原子操作确保一个任务在任一时刻最多由一个有效 Worker 执行，并 MUST 在并发提交语义相同的活动任务时复用同一 `task_id`。每次认领 MUST 产生递增的 claim generation，所有进度、终态和业务副作用提交都 MUST 验证当前 claim；租约过期的旧 Worker 不得写回。Agent lane 的并发上限和等待队列背压 MUST 对所有应用进程共同生效。

#### Scenario: 两个 Worker 同时认领
- **WHEN** 多个 Worker 同时尝试认领同一排队任务
- **THEN** 只有一个 Worker 获得有效租约并执行该任务

#### Scenario: 过期 Worker 恢复执行
- **WHEN** 旧 Worker 的租约已过期且任务已被新 Worker 重新认领，旧 Worker 随后恢复并尝试写回
- **THEN** 系统依据 claim generation 拒绝旧 Worker 的进度、终态和业务副作用提交

#### Scenario: 并发提交同一图片语境任务
- **WHEN** 多个请求为同一 scope、同一 Meme 指纹同时提交活动语境任务
- **THEN** 系统只保留一个活动任务并向所有提交者返回同一 `task_id`

#### Scenario: Agent 队列达到上限
- **WHEN** 所有进程合计的 Agent 运行任务和排队任务达到配置上限
- **THEN** 新任务被明确拒绝或返回现有去重任务，不因增加应用进程而绕过背压

### Requirement: 批次收束必须恰好触发一次
系统 MUST 将任务与批次关系持久化，并在一个批次的全部语境任务进入终态后只触发一次该批次的索引刷新收束操作。服务重启、任务复用或多个 Worker 并发检查不得导致遗漏或重复触发。

#### Scenario: 批次全部完成
- **WHEN** 一个批次关联的全部语境任务进入成功或失败终态
- **THEN** 系统为该批次创建且只创建一个索引刷新任务或等价收束记录

#### Scenario: 重启后恢复批次
- **WHEN** 服务在批次部分完成时重启
- **THEN** 新 Worker 根据数据库关系继续跟踪剩余任务，并在全部终态后完成一次收束
