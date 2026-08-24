## Purpose

为图片处理工作台提供按当前 scope 隔离、可重试且经过安全投影的 Job 查询和独立阶段提交 HTTP 契约，使客户端能够观察和恢复阶段任务而不能伪造图片路径、scope 或内部执行事实。

## ADDED Requirements

### Requirement: Image processing routes remain compatible

系统 MUST 继续注册图片处理 Job 列表、详情、显式重试、独立阶段单图和批量路由，保持 canonical/legacy path、method、`images`/`tasks` tags、status、参数约束和相对顺序；未注册 method MUST 返回 `405`。

#### Scenario: Canonical and legacy routes keep metadata

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** `/images/processing`、`/images/processing/{job_id}`、`/images/processing/{job_id}/retry`、`/images/stages` 和 `/images/stages/batch` 保持原 method 与 `202`/`200` status
- **AND** `/image-processing` 及其详情/重试/阶段路径继续作为隐藏旧别名
- **AND** 静态 `/images/processing` 路由在动态 `{job_id}` 路由之前，阶段 canonical route 继续先于旧别名

### Requirement: Job reads and retry stay scope-bound

Job 列表、详情和重试 MUST 只通过当前请求 scope 的 repository 读取或创建 revision；详情不存在时返回 `404/image_processing_job_not_found`，跨 scope 标识不得被观察。重试 MUST 保留旧 Job 终态并创建新 revision，成功后按当前 Worker 可用性调度并返回安全 snapshot。

#### Scenario: Missing or cross-scope Job is indistinguishable

- **WHEN** 客户端查询不存在或属于其它 scope 的 job id
- **THEN** 列表/详情不返回其它 scope 数据，详情返回 `404/image_processing_job_not_found`

#### Scenario: Explicit retry creates a new revision

- **WHEN** 客户端重试可重试状态的 Job，并省略或提供合法处理选项
- **THEN** 系统按旧 revision 的冻结选项继承缺省值、创建新 revision、调度可用 Worker 并返回公开 snapshot
- **AND** 旧 Job 不被重新激活

### Requirement: Independent stage submission is server-bound

单图阶段入口 MUST 只接受当前 scope 的 `meme_id`、canonical/legacy 阶段名和受限处理策略；图片路径、scope、目标文件名等字段 MUST 被拒绝或忽略。只允许公开阶段 alias 映射到四种内部阶段，目标图片不存在、内容变化、处理服务不可用和策略拒绝必须使用稳定错误码。

#### Scenario: Single stage validates target and policy

- **WHEN** 客户端提交单图阶段请求
- **THEN** 系统从当前 scope metadata service 派生图片和 identity，规范化阶段与处理选项后提交独立 task
- **AND** 返回 `202` 及安全 task 摘要，不返回 payload 或物理路径
- **AND** 无效阶段、图片缺失、目标变化或 Worker 不可用分别保持既有错误投影

### Requirement: Batch stage submission isolates failures

批量阶段入口 MUST 只接受 `visual`、`agent`、`text_embedding` 三种核心阶段及受限 `meme_id` 列表；重复/不允许阶段 MUST 整体返回 `422/invalid_image_stage`。每个图片/阶段组合 MUST 独立处理，单项失败不得阻止其它组合，顶层计数 MUST 仅统计实际提交的 task。

#### Scenario: Invalid batch stage is rejected before side effects

- **WHEN** 批量请求包含 `auto_rename`、重复阶段或不允许的阶段
- **THEN** 系统返回 `422/invalid_image_stage`
- **AND** Worker、metadata service 和任务 service 不被调用

#### Scenario: Batch retains partial result details

- **WHEN** 批量请求中部分图片不存在、目标变化或 Worker 单项提交失败
- **THEN** 其它有效组合继续提交
- **AND** 每项结果包含受控 `meme_id`、stage、task 摘要或稳定 error，顶层 `submitted_count` 与实际 task id 数量一致

### Requirement: Image stage module dependencies remain one-way

公共图片阶段 HTTP 模块 MUST 不导入 `api.py` 或 Server 入口；scope/service、repository、Worker、配置、错误、任务摘要和宿主 operation policy 投影 MUST 通过 callback 注入，旧入口中的请求模型、handler 和 legacy route names MUST 继续可导入。

#### Scenario: Legacy imports and dependency boundary remain available

- **WHEN** 调用方从 `api` 导入图片阶段请求模型或旧 handler
- **THEN** 这些名称仍可调用并保持原 route wrapper 语义
- **AND** 新模块静态依赖中不出现 `api` 或 `server_api`
