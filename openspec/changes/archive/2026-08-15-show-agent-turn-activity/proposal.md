## Why

语境研究任务可能持续数分钟，而现有任务页在 Agent 执行期间只停留在粗粒度阶段和固定百分比，用户难以区分“仍在工作”和“已经卡住”。OpenCode 已在共享运行目录的 SQLite 会话库中持续记录 Agent 步骤和更新时间，可以用低成本、只读且可降级的方式提供可信的活跃度反馈。

## What Changes

- 为语境研究任务的状态摘要增加可选的 Agent 活跃度字段：已完成轮次、当前轮次是否进行中、最近活跃时间。
- 从共享 `data/opencode/opencode.db` 批量只读查询任务对应 session 的 `step-start`、`step-finish` 和最新 part 更新时间；不得读取或返回推理文本、工具参数和原始日志。
- 在处理任务列表及任务详情中展示“Agent 已完成 N 轮”“第 N+1 轮进行中”和最近活跃时间，并明确这些信息是活动指示而非完成进度。
- OpenCode 数据库不存在、繁忙、权限不足或 schema 不兼容时静默省略活跃度字段，任务查询与语境研究不得因此失败。
- 保留现有任务状态轮询，不引入 SSE、WebSocket、Agent 输出流改造或新的任务持久化字段。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `task-status`：任务状态可为语境研究任务提供可选、只读且可降级的 Agent 轮次与最近活跃信息，并规定前端展示语义。

## Impact

- 后端：新增隔离的 OpenCode 活跃度只读适配器，并在任务列表/详情响应装配可选字段。
- API：任务摘要增加向后兼容的可选字段，不改变任务状态机和现有字段语义。
- 前端：处理任务列表、详情抽屉及相关测试增加 Agent 活跃度展示。
- 运行环境：依赖当前固定版本 `opencode-ai@1.18.18` 的 SQLite schema，以及宿主机可访问的 `/runtime` bind mount 来源目录；不需要访问 Docker socket或容器网络。
