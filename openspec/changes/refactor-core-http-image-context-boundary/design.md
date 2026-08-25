## Context

图片语境和视觉向量入口位于 `api.py` 图片库/媒体路由之后，负责从当前 scope 派生 Meme 与受控 BlobStore 路径，调用已有图片处理 Job 提交 facade，并把父 Job/叶子阶段投影为兼容 task response。批量入口还需要保留逐项跳过和失败隔离，metadata repair 只是 task service 的幂等提交。

## Goals / Non-Goals

**Goals:**

- 把五个图片语境/视觉/repair handler 和对应输入模型移到不依赖 `api.py` 的公共模块。
- 通过显式 callback 复用 scope-bound service、environment、处理 Job 提交、任务 service、错误工厂和稳定错误码。
- 保持输入字段拒绝、meme_id 派生、目标变化、reverse image policy、父/叶子 task 标识和批量结果语义。

**Non-Goals:**

- 不迁移图片上传、重命名、删除、媒体或合集路由。
- 不修改 `ImageProcessingWorker`、`PostgresTaskService`、metadata schema、数据库迁移、operation policy 或前端。
- 不让客户端提交文件路径、scope、目标文件名或 task/job 归属。

## Decisions

### 1. 输入模型随 HTTP 边界迁移

`ContextRequest`、`ContextBatchRequest` 迁入新模块；`api.py` 继续导入并暴露同名对象，保持 FastAPI 校验与旧 import 兼容。

### 2. 目标解析和异步提交通过 callback 注入

模块接收 `service`、`environment`、`submit_processing_job`、`task_service`、`error`、`enqueue_error` 和可选 `operation_error` callback。目标解析仍只读取当前 scope metadata service；数据库 fallback 只用于保留原 Meme 存在但 sidecar 读取失败时的排队语义，明确的指纹不一致仍返回 `target_changed`。宿主可通过 `operation_error` 保留 `Retry-After` 等策略投影。

### 3. 批量保持逐项隔离

批量语境和视觉入口逐项执行 metadata status、repair pending 和 Job submit；单项错误只返回受控 code，后续项继续处理。`include_unready=False` 的已就绪图片继续返回 `skipped: already_ready`。

### 4. 任务 response 由既有 snapshot 投影组成

父 Job/视觉阶段的 task_id、processing_job_id、submission_mode 和 status 继续从现有 snapshot 派生。新模块不复制数据库/Worker 状态机，也不公开 payload、路径或 scope。

## Risks / Trade-offs

- [scope callback 绑定错误] -> 测试确认 service/environment callback 获得原始 Request，且模块不导入入口。
- [批量异常泄漏或中断] -> 对 MetadataError、HTTPException、OSError、RuntimeError 做既有稳定映射，并测试一项失败后后续仍提交。
- [route order/legacy import 漂移] -> snapshot 测试断言五个 canonical route 的 method/status/tag/order 和 api re-export。

## Migration Plan

1. 在开源仓库新增模块、OpenSpec artifacts 和契约测试，迁移 handler wrapper。
2. 运行图片处理、API、scope、安全相关测试、compileall、strict validate 和完整非外部门禁，提交精确 SHA。
3. 用户已批准本地精确同步；Server fetch 该 SHA 后普通 `--no-ff` merge，解决只限入口兼容冲突并运行 Server 定向回归。
4. 回滚时恢复 `api.py` 原模型和五个 handler，删除新模块、测试和 change artifacts；不修改领域状态机。
