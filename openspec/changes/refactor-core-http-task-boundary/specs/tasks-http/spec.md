## Purpose

为任务工作区提供稳定、按当前 scope 隔离且经过脱敏的任务查询与控制 HTTP 契约，使客户端能够观察任务状态并安全执行取消或失败重试，而不接触内部 payload、路径或执行对象。

## ADDED Requirements

### Requirement: Task control routes remain compatible

系统 MUST 继续注册公开的任务列表、详情、取消和重试路由，保持原 path、method、`tasks` tag、参数约束、相对顺序以及重试的 `202` status；未注册 method MUST 返回 `405`。

#### Scenario: Route table and methods remain stable

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** 路由表包含单个 `GET /tasks`、`GET /tasks/{task_id}`、`POST /tasks/{task_id}/cancel` 和 `POST /tasks/{task_id}/retry` route
- **AND** 重试 route 返回 `202`，其它三个 route 返回 `200`
- **AND** 不匹配的 HTTP method 不执行任务 service 并返回 `405`

### Requirement: Task summaries are scope-bound and sanitized

任务列表和详情 MUST 只读取当前请求 scope 的 task/metadata/image-processing service，并返回公开任务摘要；响应 MUST 不包含任务 payload、scope 身份、物理路径或未经过公开 DTO 校验的内部结果。图片阶段任务的 stage、来源、retry 和视觉字段 MUST 按既有规则投影。

#### Scenario: List returns filtered public summaries

- **WHEN** 客户端请求 `/tasks` 并提供 status、task_type、cursor 或 limit 参数
- **THEN** 系统将过滤条件和分页参数只传给当前 scope 的 task service
- **AND** 响应包含 `items` 与 `next_cursor`，每项只包含稳定公开字段

#### Scenario: Missing task is not disclosed across scopes

- **WHEN** 客户端查询当前 scope 不存在的 task id
- **THEN** 系统按既有兼容回退查询当前 scope 的图片处理 job
- **AND** 两者都不存在时返回 `404/task_not_found`，不泄露其它 scope 的事实

### Requirement: Agent activity and resume diagnostics fail closed

任务摘要 MAY 包含 Agent 活跃度和有限续跑诊断，但活跃度读取失败或字段不完整时 MUST 只省略这些字段；resume 开关关闭、次数/累计时间耗尽、标识非法时 MUST 将 `resume_available` 投影为 `false` 并提供稳定原因码，不得暴露执行路径。

#### Scenario: Activity reader failure preserves task success

- **WHEN** Agent 活跃度 reader 抛出异常或返回不完整值
- **THEN** 任务列表和详情仍返回原任务摘要成功响应
- **AND** 不返回部分活跃度字段

#### Scenario: Resume visibility is bounded by policy

- **WHEN** 任务持久化为可续跑但设置关闭续跑、预算耗尽、累计超时或标识不合法
- **THEN** 响应中的 `resume_available` 为 `false`
- **AND** `resume_reason` 仅为稳定公开原因码，session/attempt 只保留合法 opaque 标识

### Requirement: Cancel and retry preserve task service semantics

取消 MUST 只对当前 scope 的未完成任务执行一次 task service cancel，并在 Agent 任务上调用可选的取消适配器；已完成任务 MUST 直接返回摘要。重试 MUST 只调用 task service retry，并将 `task_not_found`、`task_not_failed`、`image_stage_retry_forbidden`、`agent_backpressure` 等稳定错误映射为既有 HTTP status 和 error code。

#### Scenario: Cancel does not affect unrelated execution

- **WHEN** 客户端取消一个未完成任务
- **THEN** 系统只取消该 task id，并返回当前任务公开摘要
- **AND** 不停止共享 Agent 容器或其它 task/session

#### Scenario: Retry rejects non-failed or forbidden image task

- **WHEN** task service retry 报告任务不存在、任务未失败、图片阶段禁止通用重试或 Agent backpressure
- **THEN** 系统分别返回 `404`、`409`、`409` 或 `429`，并使用稳定 error code
- **AND** 不直接暴露底层异常文本

### Requirement: Task HTTP module has one-way dependencies

公共任务 HTTP 模块 MUST 不导入 `api.py` 或 Server 入口；scope/service、错误、图片处理 repository 和 Agent 取消能力 MUST 通过入口 callback 注入，旧入口中的任务符号 MUST 继续可导入和调用。

#### Scenario: Legacy handler imports remain available

- **WHEN** 调用方从 `api` 导入任务 handler 或 `_task_summary`
- **THEN** 这些名称仍然可调用并保持原 route wrapper 语义
- **AND** 新模块的静态依赖中不出现 `api` 或 `server_api`
