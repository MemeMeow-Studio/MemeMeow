# Agent Session 续跑运维说明

Agent session 自动续跑由 `MEMEMEOW_AGENT_RESUME_ENABLED` 独立控制，默认值为
`false`。该开关不会关闭既有的任务级自动重试；关闭时失败任务仍按原有重试策略
执行，但不会把 OpenCode session 作为续跑目标。

启用前应确认以下配置已经按部署环境设置：

- `MEMEMEOW_AGENT_RESUME_MAX_ATTEMPTS`：单个业务 Task 最多续跑次数。
- `MEMEMEOW_AGENT_RESUME_BACKOFF_SECONDS`：首次恢复前的退避秒数。
- `MEMEMEOW_AGENT_RESUME_MAX_BACKOFF_SECONDS`：指数退避上限。
- `MEMEMEOW_AGENT_RESUME_TIMEOUT_SECONDS`：所有续跑 attempt 共用的总时间上限。

只有明确绑定到同一业务 Task、scope、图片 SHA-256、处理配置和失败 attempt 的
session 才能续跑。429、5xx、连接中断和可证明的进程级暂态错误可以进入续跑；
`unknown_execution`、输入变化、结果文件损坏、授权或计量状态不明的任务必须由
用户显式创建新的处理 revision。续跑使用新的 executor attempt ID，不得复用已终态
ID，也不得使用 OpenCode 全局“继续最近会话”语义。

任务接口中的 `resume_available`、`resume_reason`、`session_id`、
`executor_attempt_id`、`resume_attempts`、`resume_started_at`、`first_error` 和 `error_history` 只包含有限脱敏诊断。图片
处理 Job 的阶段摘要会同步展示当前 attempt 的 session/恢复状态；不会返回 prompt、
工具参数、密钥或完整 transcript。续跑开关关闭时，即使数据库保留旧的恢复事实，
任务详情也会返回 `resume_available=false`、`resume_reason=resume_disabled`；达到
续跑次数或累计时间上限时返回 `resume_reason=resume_budget_exhausted`。额度未耗尽的
失败任务在自动恢复窗口内可以暂时显示为 `queued`，表示 Worker 正在按退避策略
继续同一业务 Task，而不是用户显式重试、成功或把任务误报为 `running`。只有恢复
被禁止、session 不可验证或续跑次数/累计时间预算耗尽后，任务才最终收束为
`failed` 或 `unknown_execution`。

回滚时将 `MEMEMEOW_AGENT_RESUME_ENABLED` 设为 `false` 并重启 Worker 即可。数据库
中的 session、attempt 和错误历史保留，不需要手工删除；需要重新执行时使用任务或
图片处理 Job 的显式重试接口创建新的逻辑 revision。
