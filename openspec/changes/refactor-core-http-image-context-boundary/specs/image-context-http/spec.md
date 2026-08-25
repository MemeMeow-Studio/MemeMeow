## Purpose

为图片工作区提供 scope-bound 的语境、视觉向量和 metadata repair HTTP 编排，使客户端能够创建或观察图片处理任务而不能伪造路径、范围或内部任务事实。

## ADDED Requirements

### Requirement: Image context routes remain compatible

系统 MUST 继续注册图片语境单图/批量、视觉向量单图/批量和 metadata repair 路由，保持 canonical path、method、`images`/`tasks` tags、status、请求字段和相对顺序；未注册 method MUST 返回 `405`。

#### Scenario: Route snapshot remains stable

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** `/images/context`、`/images/visual-embedding`、`/images/visual-embedding/batch` 和 `/images/metadata/repair` 继续使用原 method 与 `202` status，`/images/context/batch` 保持原 `200` status
- **AND** route handler 名称和 `api` 中的旧请求模型 import 继续可用

### Requirement: Operation policy errors keep host projection

图片语境 Job 提交遇到 operation policy 拒绝时 MUST 通过可选宿主 callback 投影；未提供 callback 的公共调用仍返回既有稳定错误 code/status，Server 入口 MUST 注入自己的 `Retry-After`/`retry_at` 适配。

#### Scenario: Context policy limit keeps retry metadata

- **WHEN** 图片语境提交收到 `operation_limit_exceeded` 且宿主提供 operation error callback
- **THEN** callback 收到同一 code 与 retry_at，响应保留宿主定义的 `Retry-After`

### Requirement: Targets stay scope-bound

单图和批量入口 MUST 只通过当前 scope metadata service 派生 Meme 与受控图片，不接受路径、scope、用户或目标文件名字段。缺少 meme_id、图片不存在、目标指纹变化和处理选项不可用 MUST 保持稳定错误 code。

#### Scenario: Target cannot be selected by path

- **WHEN** 请求包含额外 `path`、`scope_id` 或未知 JSON 字段
- **THEN** Pydantic 请求校验拒绝请求，且不调用 metadata service 或任务提交 callback

#### Scenario: Existing Meme with missing sidecar remains enqueueable

- **WHEN** metadata sidecar 读取失败但当前 scope 的 Meme 仍存在
- **THEN** 单图语境入口按原兼容行为从 repository 读取 Meme 并交给已有处理 Job facade，目标变化由控制面在 claim 时收束

### Requirement: Batch work isolates failures

批量语境和视觉入口 MUST 逐项处理；已就绪且 `include_unready=false` 的项目返回 `skipped=already_ready`，单项 metadata、目标或排队失败不得阻止后续项目，结果只包含受控 meme_id、task/job 标识、status 或稳定 error code。

#### Scenario: One failure does not abort later work

- **WHEN** 批量项目中一项图片不存在或任务提交失败
- **THEN** 该项返回稳定错误，后续有效项目仍调用处理 Job callback

### Requirement: Repair submission stays idempotent and scope-bound

metadata repair MUST 只通过当前 scope task service 提交 `metadata_repair` 空 payload，返回 task_id、task_type 和 status，不接受客户端 body 或 scope 选择器。

#### Scenario: Repair uses the bound task service

- **WHEN** 当前 scope 请求 POST `/images/metadata/repair`
- **THEN** 系统只调用注入的 task service 提交 `metadata_repair` 与空 payload
- **AND** 响应不包含 scope、路径或内部任务 payload

### Requirement: Context module dependencies remain one-way

公共图片语境 HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；所有可变应用依赖 MUST 通过 callback 注入，旧模型和 handler MUST 继续从 `api` 导入。

#### Scenario: Dependency and compatibility boundary

- **WHEN** 调用方从 `api` 导入 `ContextRequest`、`ContextBatchRequest` 或五个旧 handler
- **THEN** 名称仍可调用并保持 wrapper 语义
- **AND** 新模块静态依赖中不出现 `api` 或 `server_api`
