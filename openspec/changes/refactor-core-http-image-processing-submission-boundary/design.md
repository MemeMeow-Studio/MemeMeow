## Context

`POST /images/processing` 由当前 scope 分页枚举 Meme，读取 metadata/embedding 输入，向
图片处理 Worker 提交或复用 Job，并逐图隔离错误。客户端只能提交联网策略和 auto-name，
图片、scope、Job 归属和 retry 事实由当前 scope repository/worker 决定。

## Goals / Non-Goals

**Goals:**

- 将批量图片处理提交的 HTTP 编排移到不依赖 `api.py` 或 `server_api` 的公共模块。
- 通过显式 callback 注入 Worker、选项规范化、Repository、metadata service、environment、
  config 和错误工厂，保持请求顺序和旧 response。
- 保持 worker 不可用、选项错误、逐图 metadata/数据库/worker 错误和 Job 复用字段。

**Non-Goals:**

- 不迁移 ImageProcessingWorker/Repository、Job 状态机、任务调度、数据库 schema 或其它处理路由。
- 不改变 quota/operation policy、scope middleware、Server adapter 或 frontend。
- 不接受客户端提交 Meme 路径、scope、task/job ID 或执行 attempt。

## Decisions

### 1. Route/query/model 留在入口

FastAPI `Query` 声明、`ProcessingBatchRequest` 和原 route decorator 留在 `api.py`；新模块只
接收已校验 payload/page/page_size，避免 route metadata 漂移。

### 2. 控制面依赖通过 callback 注入

Worker、Repository、metadata service、environment 和 config 均由入口按当前 scope 提供；模块
只负责每项的读取、submit、复用判断和公开投影，不构造持久化或运行时资源。

### 3. 单项错误隔离保持

`ImageProcessingError` 保留稳定 code；数据库、metadata、runtime 异常统一为
`image_processing_failed`，一项失败不能阻止同页其它 Meme 提交。

## Risks / Trade-offs

- [scope/目标被客户端覆盖] -> 测试 payload 只传策略/auto-name，service 目标由当前记录派生。
- [Worker 不可用仍写入任务] -> 测试 readiness 在选项规范化后、repository/metadata 前 fail-closed。
- [复用/retry 字段漂移] -> 测试旧 Job status、同 ID reused 和 202 response 字段。

## Migration Plan

1. 在开源仓库新增模块、契约测试和 OpenSpec artifacts，保留 `api.py` 薄 wrapper。
2. 运行图片处理/API/scope/security 回归、compileall、strict validate 与 diff check，提交精确
   实现及验证记录 SHA。
3. 按已批准的精确 SHA fetch 并普通 `--no-ff` merge 到 Server，再运行 Server 定向回归。
4. 回滚时恢复 `api.py` 原 handler，删除新模块、测试和本 change artifacts，不修改 Job schema。
