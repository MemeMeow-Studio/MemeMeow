## Why

OpenCode 在模型网关暂时不可用、进程异常退出或连接中断后，当前任务只能以失败结束；后续 Worker 重试会复用同一个 executor 任务标识，可能得到 `task_exists`，也会丢失已经完成的 Agent 工作上下文。需要保留失败会话并在能够确认安全的情况下从原会话继续，避免重复执行已完成的工具调用和外部副作用。

## What Changes

- 为 Agent 任务持久化 OpenCode session 标识，即使 OpenCode 以非零状态退出也必须尽力保存。
- 为可恢复的模型网关、网络和进程级暂态错误增加基于明确 session ID 的续跑路径。
- 续跑时使用 attempt 级 executor 标识，避免复用已存在的终态 executor 任务 ID。
- 续跑必须保留任务专属 draft 和已验证的中间产物，并禁止使用无明确 session 的全局 `--continue`。
- 对无法证明外部调用是否发生、结果文件已损坏或输入已变化的失败，收束为不可自动续跑状态，等待用户显式重试。
- 任务状态和错误历史必须区分首次外部失败与后续调度失败，避免 `task_exists` 覆盖原始诊断。

## Capabilities

### New Capabilities

- `agent-session-resume`: 定义 Agent 会话标识持久化、失败分类、按 session 续跑和副作用安全边界。

### Modified Capabilities

- `task-status`: 修改失败任务的恢复语义和可诊断字段，补充可续跑状态、session 关联和续跑后的 attempt 事实。

## Impact

- 影响 `executor/server.py`、`backend/agent_executor.py`、`backend/opencode.py`、`backend/pg_services.py`、`backend/database.py` 以及任务状态 API。
- 需要扩展任务和图片处理 attempt 的持久化字段或结果摘要，并增加 executor、Worker、任务状态和恢复矩阵测试。
- 不修改 OpenCode 二进制或其 `opencode.json`；续跑由 MemeMeow 的受控 executor 调度实现。
- 现有终态任务和显式完整重试语义保持兼容；不可安全续跑的任务仍必须由用户显式创建新的处理 revision。
