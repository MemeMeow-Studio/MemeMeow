## 1. 并发探针与配置

- [x] 1.1 为 OpenCode 并发上限、Agent lane 资源配额和背压阈值增加环境配置、脱敏状态和默认值；默认 Agent 并发保持为 `1`。
- [x] 1.2 编写共享 OpenCode DB、workspace、loopback session messages API、只读 skill/dependency 在并发运行下的探针与诊断输出。
- [x] 1.3 根据探针结果实现共享 runtime 或固定 slot runtime 的初始化与迁移检查，禁止任务期间创建临时依赖或运行包管理器。

## 2. Agent 调度与跨进程互斥

- [x] 2.1 将 context 任务从通用线程池中分离到受并发上限控制的 Agent lane，并为超限任务实现可观测的 queued/backpressure 状态。
- [x] 2.2 将全局 `worker.lock` 改为经验证的 slot 级跨进程互斥，同时保留进程内 semaphore，确保多应用进程不会重复占用同一 slot。
- [x] 2.3 保持活动任务按 task type 与规范化 payload 去重，验证同一图片不会启动第二个 OpenCode 或重复调用同键 reverse-image provider。
- [x] 2.4 保证 cache generation 和 metadata repair 在 Agent lane 满载时仍能获得保留资源，并维持任务状态、稳定排序和重启恢复语义。

## 3. 安全写回与缓存协调

- [x] 3.1 保持每个并行 job 的独立 session、最后 assistant JSON 提取、schema/Pydantic 校验、前后 SHA-256 校验和 sidecar 原子写回顺序。
- [x] 3.2 为并行自动命名增加目标路径冲突保护；图片或 sidecar 移动失败时只记录命名错误，不撤销已验证的语境写回。
- [x] 3.3 为语境任务增加批次或合并失效标记，在批量任务终态后提交去重的 cache generation，避免逐图重建全库 embedding。
- [x] 3.4 为超时、取消、应用关闭和重启清理受管理的进程组，并将无法确认的 running 任务持久化为 `task_interrupted`。

## 4. 测试与文档

- [x] 4.1 增加并发度为 `1` 的兼容性测试，以及并发度为 `2` 时不同图片独立 session、并行执行和单项失败隔离测试。
- [x] 4.2 增加跨进程 slot 锁、任务去重、背压、公平调度、服务重启、孤立进程和 provider 限流测试。
- [x] 4.3 增加 sidecar 目标变化、自动命名冲突、并行写回不互相覆盖和缓存合并失效测试。
- [x] 4.4 更新 OpenSpec、配置说明、API/任务页文档，说明默认单并发、并发上限、回退方式和资源/隐私边界。
- [x] 4.5 在测试 runtime 完成两张以上图片从排队、并行 Agent、sidecar ready 到统一 v4 embedding cache 的端到端验收，并执行完整测试与严格 OpenSpec 校验。
