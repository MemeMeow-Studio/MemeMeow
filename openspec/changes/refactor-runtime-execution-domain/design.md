## Context

当前代码已经具有持久 Task、图片处理 Job、scope-aware workspace、executor attempt 和结果文件隔离等行为，但 `backend/image_processing.py`、`backend/opencode.py` 与 `executor/server.py` 同时包含状态模型、调度、外部副作用和协议适配。详见 [proposal.md](proposal.md)。

## Goals / Non-Goals

**Goals:**

- 让每个长任务只有一个可识别的生命周期 owner，并让公共 facade 只组合窄的 domain/runtime 接口。
- 把逻辑 Task 与执行 attempt 分离；attempt 必须绑定 task、scope、图片版本、workspace selector 和输入摘要，旧 attempt 不能写回新执行。
- 把图片阶段推进限制为固定有序计划；阶段业务处理不直接创建后续阶段。
- 把 OpenCode 的 workspace 解析、session 标识、子进程收束和结果文件校验放到可独立测试的职责模块。
- 保留旧 import/re-export、任务状态字段、service DNS 和现有持久事实。

**Non-Goals:**

- 不引入通用 DAG、插件式工作流、消息队列或第二套数据库任务表。
- 不改变账户、quota、operation policy、数据库 schema/migration、HTTP 路由或前端协议。
- 不把 Server 专属 project name、端口、cookie 或环境变量写入公共核心。

## Decisions

### 1. 采用四层依赖方向

依赖方向固定为：`domain contracts -> repositories/control plane -> orchestrators -> runtime adapters -> entrypoints`。domain 层只保存不可变标识、状态转换和错误分类；repository/control plane 负责持久事实；orchestrator 负责短事务 reconcile；runtime adapter 负责 OpenCode/子进程外部副作用；入口只装配依赖。

曾考虑把全部代码复制到新目录再逐步切换，但会产生两份状态收束逻辑和长期漂移；本变更使用窄新模块、旧模块 re-export 和渐进接线。

### 2. 逻辑任务与执行 attempt 分离

新增不可变 attempt 记录值对象和 claim 校验函数。一次逻辑 Task 可以因租约恢复拥有多个 attempt，但一次 attempt 只允许一个 owner；取消、超时、进程未 reaped 或结果不完整都通过稳定错误码收束。外部调用已发生但结果无法证明时，统一为 `unknown_execution`，不得自动重放副作用。

### 3. 图片阶段使用固定计划

图片域只暴露 `visual -> agent -> text_embedding`（以及现有独立 auto-rename）的固定 stage plan。Job repository 读写 scope、Meme SHA、配置和 metadata 指纹；stage plan 只判断有效性与下一阶段；Worker 负责 claim/attach/submit/reconcile；叶子 handler 只执行一个 stage。现有 `ImageProcessingWorker` 保留为兼容 facade。

### 4. Executor 以 supervisor、result store、protocol 分层

executor 的任务状态/请求校验/结果文件协议与进程 supervisor 分开。supervisor 是唯一能启动、等待、终止和确认 reaped 的组件；result store 只接受受控 task directory、原子临时文件、大小/符号链接检查和 schema 校验；HTTP handler 只解析受限字段并调用 facade。

## Risks / Trade-offs

- [新 facade 与旧类并存可能造成边界再次模糊] -> 所有新调用优先依赖窄协议；旧类只作为 re-export/组合入口，新增测试禁止直接依赖内部进程或 payload scope。
- [attempt 收束更严格会暴露旧任务的未知状态] -> 保留稳定 `unknown_execution` 和显式重试入口，不把未知状态伪装成可自动重放的失败。
- [公共模块先提交再被 Server 同步会遇到适配差异] -> 公共提交只包含公共路径，Server 通过精确 SHA 的普通 merge 接入。

## Migration Plan

1. 加入公共 contracts、图片 stage plan、OpenCode attempt/result 边界、兼容接线和黑盒测试，运行 strict validation、compileall、受影响及完整后端测试。
2. 形成唯一域级 Git commit，记录精确 SHA、范围和验证结果；不访问 upstream、不 push。
3. 用户批准 Server 同步后，Server 通过精确 SHA 普通 `--no-ff` merge；Server 专属 runtime identity 留在适配层。

## Open Questions

无。
