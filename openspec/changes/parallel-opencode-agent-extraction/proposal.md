## Why

当前持久任务服务虽然使用两个线程，但 `OpenCodeRunner` 通过进程内 `_run_lock` 和跨进程 `worker.lock` 将所有图片语境提取强制串行。批量补齐时，第二个任务会占用任务线程却只能等待锁，导致 Agent 吞吐受限，并可能阻塞缓存生成和 metadata repair。图片之间已经拥有独立 session、独立目标指纹和独立 sidecar 提交边界，适合在验证运行时并发安全后采用受控并行。

## What Changes

- 将 OpenCode 语境生成从全局单并发改为可配置的受控并发；默认值保持为 `1`，并提供明确的最大并发上限和快速回退路径。
- 为不同图片的 Agent 任务提供公平调度和背压，避免 Agent 任务占满线程后使 cache generation 或 metadata repair 饥饿。
- 重新定义跨进程 worker 互斥，确保多个应用进程不会重复占用同一个并发 slot；同一图片的活动任务仍必须去重。
- 在确认共享 OpenCode DB、session API 和 workspace 支持并发前，提供并发能力探针和隔离方案；不得把未经验证的 SQLite 并发行为作为隐含前提。
- 保持单张图片内部的研究顺序、最后 assistant JSON 提取、前后 SHA-256 校验、sidecar 原子写回和人工字段保护不变。
- 合并并发任务产生的检索缓存失效信号，在一批语境写回完成后显式触发一次缓存生成，避免每张图片重复重建全库 embedding。
- 对自动命名、同目录目标冲突、超时、取消、服务重启、供应商限流和第三方 reverse-image 查询增加并发下的隔离与诊断。
- **BREAKING**：`meme-search` 的 OpenCode 运行时要求从“同一实例最多一个语境生成子进程”改为“同一实例最多运行配置数量的受控子进程”，但默认配置仍为单并发以保持兼容。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `meme-search`: 修改 OpenCode Agent 任务的并发、调度、runtime 互斥和批量缓存失效要求，同时保留每图独立 session 与安全写回契约。
- `task-status`: 补充并发任务的公平调度、背压、资源占用和终态一致性要求，确保并行执行不改变任务查询与去重语义。

## Impact

- 影响 `backend/opencode.py`、`backend/tasks.py`、`api.py`、`backend/config.py` 及相关 OpenSpec 规范和 API 文档。
- 需要验证 OpenCode CLI、共享或分 lane 的 OpenCode DB、loopback session messages API、workspace 只读依赖和跨进程文件锁的并发行为。
- 需要新增并发、去重、跨进程互斥、背压、缓存合并失效、自动命名冲突、超时取消、重启恢复和供应商限流测试。
- 不改变上传异步返回、任务查询接口、sidecar schema、Agent 输出校验或 embedding 白名单；默认并发为 `1` 时应保持现有行为。
