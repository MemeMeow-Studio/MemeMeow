## Context

当前 `api.py` 在 `/generate-cache` 之后定义四个任务控制路由，并在它们之前实现 `_activity_payload`、`_read_agent_activity` 和 `_task_summary`。摘要逻辑依赖 `TaskRecord` 的公开 DTO、Agent resume 策略、图片处理 repository、metadata service 及有限的活跃度观测；真实请求的 service 只能从当前 scope 读取。

## Goals / Non-Goals

**Goals:**

- 将任务 HTTP 编排和公开摘要投影移到不依赖 `api.py` 的公共模块。
- 通过显式 callback 复用入口的 scope/service、错误、图片处理 repository 和 OpenCode 取消边界。
- 保留动态路由前的静态任务路由顺序、参数校验、响应投影、错误码和旧 import 行为。
- 让活跃度 reader 的异常仍只隐藏诊断字段，不改变任务 API 的成功/失败语义。

**Non-Goals:**

- 不移动或重构 `TaskRecord`、任务持久化、worker、图片处理 repository 或 Agent resume 算法。
- 不改变任务状态机、scope middleware、数据库事实、权限/quota 语义或任务响应 DTO。
- 不迁移图片处理、普通图片、callback、合集或其它任务提交路由。

## Decisions

### 1. 任务域使用显式依赖注入

`backend/task_http.py` 接收 `service(request, name)`、`error(status, code, message)`、`processing_repository(request)` 和 `cancel_agent(request, task_id)` callback。模块不导入 `api.py`、`server_api` 或入口全局对象；入口 wrapper 负责绑定这些依赖。与把入口 helper 作为公共模块依赖相比，显式 callback 能保留 scope 选择和适配层替换点。

### 2. 摘要投影与路由 handler 同模块迁移

`_task_summary` 不是单纯的 DTO helper：它读取 metadata、图片处理阶段和 settings，并附加 Agent 活跃度及续跑策略。将其与四个 route handler 一起迁移能保持所有调用路径使用同一投影。`api.py` 继续 re-export `_activity_payload`、`_read_agent_activity` 和 `_task_summary`，兼容已有测试和外部调用方。

### 3. 任务详情的图片处理回退保留在 HTTP 边界

`GET /tasks/{task_id}` 在普通 Task 不存在时仍查询当前 scope 的图片处理 repository，并把父 Job 投影为旧客户端所需的视觉任务摘要。repository 查询失败按旧行为收束为任务不存在；不把图片处理领域对象或 scope 字段暴露到新模块之外。

### 4. 取消和重试只注入最小副作用能力

取消 route 仅在任务类型为 Agent 任务时调用可选的 `cancel_agent` callback；取消共享容器之外的其它 session 仍由 OpenCode 适配层决定。重试 route 保留任务服务错误码到 HTTP status/message 的映射，不在 HTTP 模块复制 retry 状态机。

## Risks / Trade-offs

- [活跃度/metadata 依赖遗漏] -> 通过单向依赖检查、摘要安全测试和现有 API activity/public DTO 测试覆盖 callback 访问边界。
- [route 顺序改变导致动态路径捕获] -> 保存原 decorator 位置，在模板路由快照中同时断言四个任务路由的相对顺序与 method/status。
- [迁移过程中旧 import 失效] -> 入口保留薄 wrapper 和兼容 aliases，并增加从 `api` 导入旧符号的回归测试。
- [开源与 Server 漂移] -> 只在开源仓库形成实现与验证 commit，Server 通过本地精确 SHA fetch 后执行普通 `--no-ff` merge。

## Migration Plan

1. 在开源仓库新增模块、迁移 wrapper、补充契约测试并运行受影响测试与完整非外部门禁。
2. 提交实现 commit，再提交验证记录 commit；核验实现 SHA、验证记录 SHA 及祖先关系。
3. 在用户授权的本地同步门禁下，Server 从开源本地历史 fetch 精确 SHA，普通 merge 并运行 Server 定向回归。
4. 回滚时恢复 `api.py` 原任务 helper/handler，删除 `backend/task_http.py`、测试和 change artifacts；不修改任务领域。
