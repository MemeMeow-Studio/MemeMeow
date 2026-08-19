## Context

当前 Agent executor 的任务 ID 同时承担业务任务标识和一次进程提交标识。executor 会保留终态任务历史，并拒绝再次提交同一 ID；OpenCode 失败时，Runner 又只在成功退出后解析 session，因此模型网关暂态错误会先丢失会话关联，随后被同 ID 的 Worker 重试转化为 `task_exists`。图片处理 Worker 还需要遵守 grant、claim fencing 和 `unknown_execution` 语义，不能简单地重复调用外部模型。

## Goals / Non-Goals

**Goals:**

- 持久化失败 OpenCode session，并能在安全边界内从明确 session 续跑。
- 将业务 Task ID、executor attempt ID 和 OpenCode session ID 分离。
- 保留首次外部错误、续跑 attempt 和最终错误，改善诊断。
- 续跑不重复已验证产物、不绕过 scope/claim/grant 校验。

**Non-Goals:**

- 不修改 OpenCode 二进制的内部重试上限。
- 不对 `unknown_execution`、输入变化、结果校验失败或计量状态不明的任务自动重放。
- 不改变用户显式完整图片处理重试创建新 Job revision 的既有契约。

## Decisions

### 1. 在 MemeMeow executor 层实现续跑

由 Runner/executor 控制续跑，而不是依赖 OpenCode 的全局 `--continue` 或修改 OpenCode 配置。executor 能同时检查任务 scope、图片相对路径、结果目录和认证，适合成为恢复边界；OpenCode 只接收明确的 session ID。

备选方案是维护 OpenCode fork 并新增 retry 配置，但会把业务任务幂等、计量和恢复问题继续留在外部工具之外，且升级成本更高。

### 2. 分离三个标识

- 业务 Task ID：PostgreSQL 中稳定的逻辑任务标识。
- Executor attempt ID：每次新进程提交的唯一标识，不能复用终态历史。
- OpenCode session ID：同一会话上下文的恢复标识。

任务状态和 `image_processing_attempts` 继续以业务 Task/claim 为主键关联 attempt；executor 内存字典只使用 executor attempt ID。恢复窗口内的暂态失败可暂时把业务 Task 放回 `queued`，由同一 Task 按预算续跑；恢复完成后，业务 Task 仍只收束一次成功或失败终态。

### 3. 在进程退出路径捕获 session

OpenCode 输出采用流式临时文件承接。Runner 在进程运行期间和非零退出后都扫描有限 JSON 事件，提取 session ID；若只能从运行时数据库读到 session，则仅在任务标题、scope、输入摘要和最近时间全部匹配时接受该 ID。无法绑定时不猜测，直接进入不可续跑状态。

### 4. 用错误分类决定续跑

允许续跑的初始集合只包含明确的模型网关 429/5xx、连接中断和 executor 进程级暂态错误，并受总 attempt 和总时间上限限制。`unknown_execution`、grant 已提交但外部结果未知、目标 SHA 变化、结果文件损坏或 schema 校验失败均禁止自动续跑；这些情况沿用显式新 revision 路径。

### 5. 续跑保留 draft，结果仍原子接收

续跑不得执行会删除已有 draft 的普通“新任务初始化”清理。每次 attempt 使用新的临时 stdout/stderr 和 executor 记录；最终结果仍必须写入固定任务目录并经过 JSON、schema、目标版本和 claim 校验后原子接收。

### 6. 诊断采用追加历史而非覆盖

任务主错误字段继续返回最终收束错误以保持兼容；新增有限的首次错误、attempt 摘要、session 是否可恢复和恢复原因。历史只保存脱敏稳定码、时间、HTTP 状态和短消息，不保存 prompt、工具参数、密钥或完整 transcript。

### 7. 用 queued 表示自动恢复中间态

对已验证 session 的暂态错误，Worker 在恢复次数和累计时间预算未耗尽时将当前 claim 重新排入 `queued`，并保留 `resume_available` 与退避信息；这只表示受控恢复尚未完成，不表示普通用户重试、成功或持续占用 `running` 租约。恢复被禁止或预算耗尽后，任务才进入最终 `failed`/`unknown_execution` 终态。

## Risks / Trade-offs

- [Risk] 续跑可能重复一次尚未确认是否生效的模型请求。→ 只对可证明没有业务副作用的 LLM 会话错误续跑；外部执行窗口未知时使用 `unknown_execution` 并要求显式重试。
- [Risk] session 数据库与 PostgreSQL 任务事实短暂不一致。→ 以任务输入摘要、scope、标题和时间窗口做绑定校验，绑定失败时拒绝续跑。
- [Risk] 旧 executor 进程仍持有同一业务任务的进程。→ attempt ID 独立、claim generation fencing 和结果写回校验共同拒绝旧 attempt 的晚到结果。
- [Risk] 旧版本任务没有 session 字段。→ 字段采用可选兼容迁移；历史任务无 session 时保持现有显式重试和失败展示。
- [Risk] 续跑延长资源占用。→ 配置单任务续跑次数、总超时和退避上限，并在任务详情暴露有限的恢复计数。

## Migration Plan

1. 增加可选的 session/attempt 诊断字段和数据库迁移，旧行默认不可续跑。
2. 先部署只读采集版本，验证失败进程能捕获 session 且不改变现有重试行为。
3. 部署 executor attempt ID 与错误历史写入，再启用受控续跑开关，默认关闭自动续跑。
4. 观察 429/5xx、`task_exists`、`unknown_execution` 和重复结果指标后，再按环境开启续跑。
5. 回滚时关闭续跑开关；已写入的 session/attempt 历史保留，任务回到现有显式重试语义。
