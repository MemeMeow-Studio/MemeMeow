## Why

任务编排、图片阶段、OpenCode session/workspace/process 以及 executor 的进程和 HTTP 控制目前分别把多个生命周期混在同一模块中。这样会让取消、租约过期、未知执行、结果写回和运行时重启时的责任不清晰，修改一个阶段也容易改变另一个阶段的协议；现在需要在不改变现有公共 API 和持久事实的前提下建立可验证的依赖方向。

## What Changes

- 建立任务生命周期、图片处理阶段、OpenCode 外部执行 attempt、结果交付和运行时身份的窄协议，并让现有入口通过兼容 facade 使用这些协议。
- 将图片 Job/Worker/pipeline stage/attempt 的职责分开：Job 只保存编排事实，stage plan 只决定下一阶段，Worker 只负责 claim、调度和收束，叶子处理器只负责业务结果。
- 将 OpenCode session/workspace、进程 supervisor、结果 store 和 HTTP executor 分成独立运行时职责；所有写回继续使用 task、scope、workspace 和 attempt 绑定。
- 统一恢复、取消、超时、失败和 unknown execution 的收束规则，旧 Worker、旧 import 和现有状态 API 保持兼容。
- 为并发认领、attempt fencing、路径/符号链接/结果大小限制、跨 scope 隔离和重启恢复补充黑盒/契约测试与 OpenSpec validation 记录。

## Capabilities

### New Capabilities

- `runtime-execution-domain`: 定义任务、图片阶段、OpenCode attempt、executor 结果交付和运行时生命周期之间的职责边界与失败收束契约。

### Modified Capabilities

无。既有任务状态、图片处理、Agent 隔离和公共 API 契约保持不变；本变更只把实现职责移动到兼容边界后面。

## Impact

- 公共核心：`backend/tasks.py`、图片处理模块、OpenCode workspace/runner、executor 的运行时协议和相关测试。
- 不改账户/quota 业务规则、数据库 schema/migration、HTTP 路由或前端页面；这些模块只继续消费稳定的任务和运行时协议。
