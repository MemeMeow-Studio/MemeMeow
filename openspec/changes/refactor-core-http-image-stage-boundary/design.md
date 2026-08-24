## Context

当前 `api.py` 在任务控制路由之后声明图片处理 Job 的 GET/重试入口和独立阶段的单图/批量入口。它们共享 `ImageProcessingRepository`、`ImageProcessingWorker`、处理选项规范化、稳定 HTTP 错误、任务摘要和 metadata service，但不需要搬迁图片处理状态机本身。Server 入口还会把 operation policy 的限流/禁止错误映射到带 `Retry-After` 的适配层响应。

## Goals / Non-Goals

**Goals:**

- 把图片处理 Job 读取、显式重试和独立阶段提交移到不依赖 `api.py` 的公共 HTTP 模块。
- 通过显式 callback 复用当前 scope 的 repository、Worker、metadata service、配置、错误和公开任务摘要。
- 保留公开路径与隐藏旧别名、route order、Pydantic 输入限制、状态投影和 scope fail-closed 语义。
- 让公共实现为 operation policy 错误保留可选 callback，Server 可以继续使用自己的错误投影而不把适配策略写进开源核心。

**Non-Goals:**

- 不迁移 `POST /images/processing` 完整处理、`POST /images/processing/unready` 全库枚举或图片上传/列表/媒体入口。
- 不修改 `ImageProcessingRepository`、`ImageProcessingWorker`、Task 状态机、quota/operation policy、数据库 schema 或前端。
- 不让客户端提供 scope、图片路径、目标文件名或其它可替代服务端事实的字段。

## Decisions

### 1. HTTP 模型随边界迁移，入口保留 aliases

`ProcessingRetryRequest`、`ImageStageSubmissionRequest`、`ImageStageBatchItem` 和 `ImageStageBatchRequest` 放入新模块，入口从新模块导入并继续暴露同名符号。这样 FastAPI 的输入模型和 handler 共享同一归属，旧调用方仍可从 `api` 导入；`ProcessingBatchRequest` 留在入口，因为它还同时服务未就绪和完整处理入口。

### 2. 所有可变应用依赖通过 callback 注入

新模块接收 `service`、`error`、`processing_repository`、`processing_worker`、`normalize_processing_options`、`processing_config` 和 `task_summary` callback。模块只直接依赖公共图片处理错误/Worker 的 canonical stage 规范化和请求模型；不导入 `api.py`、`server_api` 或入口全局对象。

### 3. operation policy 错误保持宿主投影

独立阶段 Worker 可能返回 `operation_forbidden`、`operation_limit_exceeded` 或 `operation_policy_unavailable`。新模块接收可选 `operation_error` callback；未提供时使用现有普通错误消息和 status，Server 入口提供 `_operation_http_error` 以保留 Retry-After、retry_at 和脱敏策略。这样 source 与 Server 的可观察行为都不被重构切片改变。

### 4. 批量阶段保持逐项隔离

批量入口只允许 `visual`、`agent`、`text_embedding` 三种核心阶段，逐图片/逐阶段执行 metadata 和 Worker 提交；单项失败写入结果并继续后续项，顶层计数继续由已提交 task id 计算。批量不支持 `auto_rename`，避免把单阶段 warning 语义误扩散到批量控制面。

## Risks / Trade-offs

- [scope repository/Worker callback 绑定错误] -> 所有真实请求仍由入口 callback 解析当前 scope；契约测试记录 callback 顺序并拒绝模块反向导入入口。
- [隐藏旧别名或路由顺序漂移] -> route snapshot 同时断言 canonical/legacy path、method、status、tag 和动态路径前后的顺序。
- [Server policy 错误投影丢失] -> operation error 单独注入并增加 Server 定向安全回归；公共模块不自行生成商业 quota 信息。
- [批量部分失败被误报成功] -> 逐项结果保留稳定 error/category，submitted count 只统计有 task id 的结果。

## Migration Plan

1. 在开源仓库新增模块和 OpenSpec artifacts，迁移模型、handler wrapper 及契约测试。
2. 运行图片处理、API、安全、任务、scope 回归和完整非外部门禁，提交实现 SHA。
3. 提交独立验证记录 SHA；用户授权后 Server 从本地开源历史精确 fetch 验证记录并普通 `--no-ff` merge。
4. Server 保留 operation policy callback 的适配差异，运行 Server 定向回归并记录 merge SHA。
5. 回滚时恢复 `api.py` 原模型和 handler，删除 `backend/image_stage_http.py`、测试与 change artifacts；不修改图片处理领域。
