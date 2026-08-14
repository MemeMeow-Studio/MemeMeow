## Context

见 proposal.md。语境研究由共享 Agent 容器中的 OpenCode 1.18.18 执行，容器 `/runtime` 以读写 bind mount 对应宿主 `data/opencode`；FastAPI 后端因此可直接访问同一目录，而无需 Docker socket 或容器网络。OpenCode 使用 SQLite WAL 持续写入 `opencode.db`，`session.title` 已固定为 `mememeow-task-{task_id}`，`part.data` 中的 `step-start`、`step-finish` 及 `part.time_updated` 能表达轮次和最近活动。

当前 `OpenCodeRunner.run()` 把 CLI stdout 写入临时文件并在进程终态后解析，因此诊断 JSONL 和 session ID 都不能直接提供运行中的计数。任务页已有 2.5 秒轮询，任务列表和单任务详情均经 `_task_summary` 形成安全响应。

## Goals / Non-Goals

**Goals:**

- 在不改变 Agent 执行链路的情况下，为语境研究提供近实时的轮次和活跃时间。
- 将 OpenCode 内部 schema 依赖限制在单一只读适配器内，并让所有失败安全降级。
- 复用现有任务轮询，以一次有界批量查询装配列表数据。

**Non-Goals:**

- 不展示或持久化推理文本、工具内容、原始事件和诊断日志。
- 不把轮次换算为百分比、总轮次、剩余时间或卡死判定。
- 不改造 stdout 为流式消费，不增加 SSE、WebSocket 或 PostgreSQL 任务字段。
- 不承诺兼容未验证的 OpenCode 版本或任意内部 schema。

## Decisions

### 通过隔离的只读适配器访问 OpenCode SQLite

新增后端适配器，以 `file:...?...mode=ro` 打开 runtime 根目录中的 `opencode.db`，启用 `PRAGMA query_only = ON` 并使用很短的 busy timeout。连接必须在原目录打开，使 SQLite 正常组合主库、`-wal` 和 `-shm` 的一致快照；不得复制单个主库文件，也不得使用假定文件永不变化的 `immutable=1`。

适配器只接收一组 task ID，并返回以 task ID 为 key 的领域值：`completed_turns`、`turn_running`、`last_activity_at`。表名、JSON 路径、session 标题前缀和时间单位都封装在适配器内部，API 与任务服务不直接依赖 OpenCode schema。

选择直接读取 SQLite，是因为现有 bind mount 已提供同机数据并验证会实时更新。备选方案是流式读取 CLI stdout或启动 OpenCode HTTP server；两者都会改变执行生命周期、增加进程或网络管理，而本需求只需要计数和时间。

### 使用步骤完成数定义“轮次”

已完成轮次取匹配 session 中 `part.data.type = 'step-finish'` 的数量；进行中状态取 `step-start` 数量大于 `step-finish` 数量。最近活动取该 session 所有 part 的最大 `time_updated`，从 OpenCode 毫秒时间转换为 UTC ISO 8601。

不使用工具调用数，因为单轮可产生多个工具调用；不直接使用 assistant message 数，因为进行中的 assistant message 会提前出现且不同版本的消息落库细节更不稳定。若同一任务标题意外匹配多个 session，只采用 `time_created` 最新的 session，避免跨尝试累加。

### 在 API 摘要层批量装配可选字段

任务状态自身仍由 PostgreSQL `TaskRecord` 提供，Agent 活跃度不是任务事实源，也不写回 `TaskRecord`。任务列表先取得当前页记录，再一次查询其中所有 `meme_context_generation` 的 task ID，随后将结果装配进各摘要；单任务接口复用同一批量方法传入一个 ID。

三个字段仅在存在匹配 session 且查询成功时一起返回：

```json
{
  "agent_completed_turns": 18,
  "agent_turn_running": true,
  "agent_last_activity_at": "2026-08-13T12:23:57Z"
}
```

任何 `sqlite3.Error`、文件缺失、权限错误、schema/JSON 能力不兼容或时间解码错误都在适配器边界转为“无活动数据”，记录有限诊断但不得令 `/tasks` 或 `/tasks/{id}` 失败。查询不读取 `part.data` 中除 `$.type` 外的内容。

### 沿用任务轮询并克制展示

任务列表的语境研究行在已有进度信息附近显示紧凑活动摘要；详情抽屉在“阶段”之后显示完整摘要。文案以“已完成 N 轮”“第 N+1 轮进行中”和相对时间表达，不出现 `N/M`。相对时间基于客户端当前时间计算，并在任务轮询时刷新；字段缺失时整段隐藏。

该方案不增加额外前端请求。页面不可见或用户离开任务页时，沿用当前停止轮询的行为，返回页面后由下一次任务列表加载恢复最新数据。

## Risks / Trade-offs

- [OpenCode 升级改变 `session`/`part` schema 或事件名称] → 固定容器版本；把 SQL 和事件常量封装在适配器中；兼容失败时隐藏功能而非影响任务。
- [SQLite WAL 正在写入时出现短暂 busy 或 I/O 错误] → 正常只读连接、短超时、单次批量快照与失败降级；不重试到拖慢任务 API。
- [任务列表轮询给 SQLite 增加读取压力] → 只查询当前页中的语境任务，使用一条批量 SQL/固定数量查询，并为 session title、session/part 关联使用现有索引条件；用性能测试约束查询次数和响应延迟。
- [“最近活跃”容易被误解为健康检查] → UI 称其为最近活动，不自动判断卡死，也不改变任务状态。
- [宿主部署没有挂载或权限不同] → 活跃度是可选增强；启动和任务执行不依赖 reader 成功。

## Migration Plan

1. 先部署只读适配器及 API 可选字段；旧前端会忽略新增字段。
2. 再部署前端展示，缺失字段时自动保持旧体验。
3. 用正在运行的真实语境研究验证轮次增长、最近活动更新及 WAL 并发读取。
4. 回滚时移除前端展示和 API 装配即可；无数据库迁移或持久数据需要恢复。
