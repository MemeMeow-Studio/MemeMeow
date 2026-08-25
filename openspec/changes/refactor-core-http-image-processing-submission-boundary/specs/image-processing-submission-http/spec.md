## Purpose

为当前 scope 分页提交图片处理 Job，保持 Worker readiness、选项边界、Job 复用/retry 和逐图
错误隔离语义。

## ADDED Requirements

### Requirement: Image processing submission route remains compatible

系统 MUST 继续注册 `POST /images/processing`，保持 `202`、`images`/`tasks` tags、分页 query、
请求字段和旧 handler import。

#### Scenario: Route metadata remains stable

- **WHEN** 应用完成装配
- **THEN** route 仍为单个 canonical POST，`page`/`page_size` 约束不变，并位于既有图片路由顺序。

### Requirement: Worker and options fail closed

handler MUST 在提交任何图片前确认当前 scope Worker 可用，并通过既有规范化逻辑校验
`reverse_image_policy` 与 `auto_name`。

#### Scenario: Worker unavailable or options invalid

- **WHEN** Worker 缺失或选项无效
- **THEN** 分别返回稳定 `503/image_processing_unavailable` 或既有选项错误，且不读取/提交图片 Job。

### Requirement: Submission is scope-bound and per-item isolated

每个 Meme、metadata、已有 Job 和 Worker submit MUST 来自当前 scope callback；单项错误不能
阻止同页其它项，客户端不得提交路径、scope、task/job 或 attempt 字段。

#### Scenario: Partial failures do not abort the page

- **WHEN** 某个 Meme metadata/数据库/Worker 调用失败
- **THEN** 对应结果只投影稳定 error，后续 Meme 继续处理，顶层保留 `count`、`total`、分页字段。

### Requirement: Job reuse and response projection remain compatible

已有 Job 的 retryable 状态 MUST 通过 `explicit_retry` 传给 Worker；相同 Job 复用时 `reused` MUST
反映真实 Job ID，成功项 MUST 返回 `meme_id`、`job_id`、`processing_job_id`、`submission_mode`、
`status`。

#### Scenario: Existing and new jobs are projected safely

- **WHEN** Worker 返回 snapshot
- **THEN** 结果保持旧字段和 boolean reused，不暴露 payload、物理路径或 scope。

### Requirement: Submission module dependencies remain one-way

公共图片处理提交模块 MUST 不导入 `api.py` 或 `server_api`；可变 Worker、Repository、metadata、
environment、config 和错误依赖 MUST 通过 callback 注入。

#### Scenario: Dependency boundary is preserved

- **WHEN** 静态检查新模块和旧 import
- **THEN** 无入口反向导入，旧 handler 仍可调用且 route 数量不增加。
