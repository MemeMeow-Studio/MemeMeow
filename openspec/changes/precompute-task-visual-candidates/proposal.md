## Why

当前视觉相似检索发生在 Agent 已经启动之后，结果会随数据库和图片语境变化，且失败会落入外部执行窗口，难以判断是否可以安全重试。需要把候选匹配变成任务启动前的后端事实，并将本次任务实际使用的候选内容冻结下来。

## What Changes

- 在 `meme_context_generation` Task 启动前由后端执行固定、有界的同 scope 视觉匹配。
- 将查询图片、模型身份、候选排序、候选图片 SHA、候选语境和哈希保存为版本化 `visual_match_snapshot`。
- 无合格候选以成功的空列表表示；查询向量未就绪、内容指纹变化、模型不一致或候选物化失败以稳定错误阻止 Agent 启动。
- 在每次 Agent attempt 中保存 snapshot 版本和哈希，恢复时复用同一 snapshot，不重新匹配。
- 在 OpenCode workspace 中增加 task-scoped、只读的候选图片目录和 manifest；候选目录不属于 Agent 可写 scratch。
- 从 Agent Skill、Runner 环境和 callback capability 中移除本地视觉匹配入口；Agent 只能读取后端已经准备好的候选 manifest 和图片。
- 公开任务 DTO 仅返回候选数量、状态和 snapshot 摘要，不暴露候选语境、物理路径、storage key 或跨 scope 标识。

## Capabilities

### New Capabilities

无。本 change 通过修改既有视觉匹配、任务状态、Agent 隔离和图片标注契约实现。

### Modified Capabilities

- `visual-similarity-search`: 匹配从 Agent callback 改为任务前置、可冻结的 snapshot。
- `task-status`: 任务增加视觉候选预计算状态和脱敏 snapshot 摘要。
- `agent-runtime-isolation`: OpenCode 只读取 task-scoped 候选视图，不能写入或调用视觉 callback。
- `image-labeling`: Agent 语境任务必须消费已准备的候选 snapshot，并保留候选为参考证据的边界。

## Impact

- 公共核心：Task/attempt 持久化字段、任务 claim 编排、视觉 repository、workspace 描述和权限规则。
- Runner 与 Skill：候选 manifest 读取、环境白名单和 prompt；删除 Agent 视觉 callback 地址注入。
- 数据库：新增 nullable snapshot/attempt 摘要字段和向前迁移，旧任务只读兼容并在 claim 时按迁移策略处理。
- Server 适配：后续在已审核公共 commit 基础上实现按 scope 安全物化候选图片、quota 计量和清理。
