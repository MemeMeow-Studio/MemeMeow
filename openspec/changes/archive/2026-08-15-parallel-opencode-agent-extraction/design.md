## Context

当前 `PersistentTaskService` 默认创建两个线程，但 `OpenCodeRunner.run()` 同时持有进程内 `_run_lock` 与跨进程 `worker.lock`，所以语境生成实际是单并发。任务服务还把 context、cache 和 repair 放进同一个线程池；当 Agent 任务等待全局锁时，会占住线程并造成其他任务类型饥饿。

每张图片已经使用独立 OpenCode session，任务 payload 记录图片相对路径和提交时 SHA-256，handler 在 Agent 前后复核指纹并通过 sidecar 服务原子写回。研究 skill 的观察、搜索、核验阶段有顺序依赖，不能把一张图片内部的步骤拆成并行流水线。检索缓存则是全库快照，不能在每张 sidecar 写回后立即重建。

## Goals / Non-Goals

**Goals:**

- 让不同图片的 Agent 任务在经过运行时安全验证后按受控上限并行执行。
- 保持默认单并发、稳定排队、活动任务去重、独立 session、目标指纹校验和 sidecar 原子写回语义。
- 将 Agent 资源与 cache/repair 资源隔离，提供背压、可观测的排队状态和安全的关闭/重启收束。
- 在一批语境写回后合并缓存失效，避免重复的全库 embedding。

**Non-Goals:**

- 不并行化单张图片内部的研究步骤，也不复用 session 让多张图片共享上下文。
- 不让 Agent 直接写入 canonical sidecar、图片库或应用代码。
- 不在本 change 中提高模型质量、修改输出 schema、改变 embedding 字段白名单或新增自动重试策略。
- 不以未经验证的 OpenCode SQLite/WAL 行为作为并发安全保证。

## Decisions

### 1. 用显式并发上限控制 Agent lane

新增独立的 OpenCode 并发配置，默认值为 `1`，并在配置解析时限制到合理的正整数范围。任务服务为 context handler 使用独立的 Agent lane；cache generation 与 metadata repair 保留独立资源，避免 Agent 长任务占满通用线程池。超过 Agent lane 容量的任务保持 `queued`，不启动进程。

替代方案是只把总线程池 `max_workers` 调大。该方案无法移除 `OpenCodeRunner` 的全局锁，反而会增加等待线程和任务饥饿，因此拒绝。

### 2. 先验证共享 runtime，再选择 slot 隔离

实现前增加并发探针，验证共享 `opencode.db`、workspace、loopback session messages API 和只读依赖在目标 OpenCode 版本下的行为。若共享 runtime 能稳定支持配置上限，则使用 slot 级文件锁协调多个子进程；若不能证明安全，则为每个并发 slot 创建固定、可复用的独立 DB/workspace，skill 和 `node_modules` 只读共享，并把 slot 标识保存到任务诊断中。

两种模式都必须由同一份配置选择，不能让单个 job 临时创建 runtime 或运行包管理器。跨应用进程的锁必须以 slot 为粒度；进程内 semaphore 只负责本进程调度，不能替代跨进程互斥。

替代方案是启动一个长期运行的 OpenCode server 并通过未知的多 session API 复用。当前项目只依赖已验证的 CLI 和 loopback API，贸然切换会扩大协议风险，因此不作为第一版实现。

### 3. 保留每图独立的提交门槛

并行只发生在不同图片之间。每个 handler 仍按“提交路径/SHA 校验 → OpenCode session → 最后 assistant JSON → schema/Pydantic 校验 → 再次路径/SHA 校验 → sidecar 原子写回 → 记录 session/hash”的顺序执行。sidecar 与图片移动使用现有受保护 API；自动命名增加目标路径预留或目录级互斥，避免同名竞态。

### 4. 用批次代号合并缓存失效

语境任务成功时只记录图片级 cache invalidation 和批次代号，不立即调用全库 embedding。批量入口或上层调用在确认一批任务进入终态后显式提交一个去重的 `cache_generation` 任务；缓存生成期间以原子快照读取 sidecar，失败时保留旧缓存。

### 5. 以稳定任务记录表达并行状态

任务列表继续按更新时间和 task ID 稳定排序，`queued`/`running`/终态契约不变。新增受控字段只记录 Agent 并发上限、当前 slot 或排队原因，禁止暴露密钥、完整 prompt、原始 transcript 和未经截断的工具输出。关闭时先停止接收新任务，再终止受管理的 OpenCode 进程组，最后把无法确认的运行记录持久化为 `task_interrupted`。

## Risks / Trade-offs

- [Risk] OpenCode DB 或 session API 在并发写入时存在锁冲突或数据串扰。→ 发布前运行并发探针；失败则自动采用独立 slot runtime，默认仍为单并发。
- [Risk] 多个 Agent 同时调用 reverse-image provider，费用或速率限制升高。→ 保留同一 cache key 的文件锁、设置全局 provider 限流和最大在途请求数，并把限流错误记录为稳定任务错误。
- [Risk] 并发完成顺序导致缓存看到不一致的 sidecar 集合。→ 只在批次终态后触发一次 cache generation，缓存生成使用临时文件和原子替换。
- [Risk] 自动命名的目标文件冲突或两个任务同时移动同一图片。→ 在写回后重新校验 SHA，并对目标路径做原子存在性检查和目录级互斥；冲突只报告命名失败，不撤销已写入 sidecar。
- [Risk] 应用进程异常退出留下孤立子进程或 slot 锁。→ 每个子进程使用独立进程组；超时、取消和 shutdown 都终止进程组，文件锁随句柄释放，启动恢复将遗留 running 记录标记为中断。
- [Risk] Agent lane 长时间占满导致用户看不到 repair/cache 进度。→ 独立资源配额、排队上限和任务页中的等待原因；必要时拒绝超出背压阈值的新批量提交并返回稳定错误。

## Migration Plan

1. 先加入并发探针、配置读取和观测指标，保持 Agent 并发为 `1`，验证现有测试与任务状态契约不变。
2. 在测试 runtime 中启用并发度 `2`，验证共享 runtime；若失败，初始化固定 slot runtime 并记录迁移方式。
3. 切换任务调度到独立 Agent lane，补充跨进程 slot 锁、去重、重启、取消、目标变化和缓存合并失效测试。
4. 先以默认并发 `1` 发布；运维确认 provider 限流、DB 锁冲突和资源占用稳定后，再通过配置逐步提高并发。
5. 出现异常时将并发配置降回 `1` 即可回退调度行为；不删除任务记录、sidecar、图片或已有缓存。
