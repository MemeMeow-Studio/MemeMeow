## Purpose

为任务与运行时执行建立单一、可验证的职责边界，使图片阶段、OpenCode 外部副作用和 executor 进程在恢复、取消、超时与失败时保持一致且不会越权写回。

## ADDED Requirements

### Requirement: 逻辑任务与执行 attempt 必须分离

系统 MUST 将客户端可观察的逻辑任务标识与每次实际外部执行 attempt 标识分开。每个 attempt MUST 绑定 task、scope、目标图片版本（如有）、workspace selector、输入摘要和 claim generation；attempt 不能被另一个 task 或新的 claim 复用。

#### Scenario: 租约恢复创建新 attempt

- **WHEN** 一个任务的旧租约过期并由新 Worker 重新认领
- **THEN** 新 Worker 使用同一逻辑 task 标识但生成新的 attempt 标识，旧 attempt 的进度、结果和副作用写回被拒绝

#### Scenario: 跨 scope attempt 被拒绝

- **WHEN** runtime 收到 task、attempt 或 workspace scope 与持久任务事实不一致的请求
- **THEN** 请求以稳定绑定错误拒绝，且不启动进程、不写入结果文件、不改变任务状态

### Requirement: 图片阶段必须由单一编排 owner 按固定顺序推进

图片处理 MUST 由一个 scope 绑定的 Job/Worker 编排 owner 推进固定阶段；叶子阶段处理器 MUST 只报告本阶段结果，不直接创建后续阶段。连续有效阶段 MUST 被复用，第一个缺失或过期阶段之后的阶段 MUST 保持不可运行。

#### Scenario: 前置阶段有效

- **WHEN** Worker 发现视觉阶段的图片 SHA、模型和预处理版本仍有效
- **THEN** 视觉阶段被标记为复用，Worker 只安排下一个需要执行的阶段

#### Scenario: 叶子阶段失败

- **WHEN** 一个阶段返回稳定失败或 blocked/unknown_execution
- **THEN** Job 停止推进后续阶段，保留已提交产物和诊断，并等待恢复或显式重试

### Requirement: 取消、超时和未知外部执行必须 fail-closed 收束

runtime MUST 由唯一 supervisor 管理子进程/远端 executor 的启动、取消、超时、reap 和终态；结果未完整校验或外部调用是否发生无法证明时 MUST 使用稳定 `unknown_execution` 收束，不能自动重放可能产生副作用的调用。

#### Scenario: 取消运行中的 attempt

- **WHEN** 用户取消一个 queued 或 running 任务
- **THEN** queued 任务不启动，running 任务被定向终止并最终进入 cancelled/task_interrupted，其他 task 和共享 runtime 继续运行

#### Scenario: 超时后进程已收束

- **WHEN** OpenCode 超过任务超时时间且 supervisor 成功 reap 进程
- **THEN** attempt 记录稳定 timeout 错误、任务进入失败或取消终态，result store 不接受不完整文件

#### Scenario: 外部状态未知

- **WHEN** supervisor 在外部调用后失联或无法确认进程/远端 attempt 的实际状态
- **THEN** 任务进入 `unknown_execution`，禁止隐式自动重试，客户端获得可诊断的显式恢复信号

### Requirement: 结果交付必须原子且绑定 workspace

result store MUST 只接受受控 workspace 下的任务专属临时文件，拒绝符号链接、路径逃逸、非普通文件、超过大小上限或 schema 无效的结果；校验通过后 MUST 通过同一文件系统的原子替换/提交交付。

#### Scenario: 结果文件路径逃逸

- **WHEN** attempt 提供绝对路径、父级跳转或符号链接结果路径
- **THEN** store 拒绝读取并返回稳定路径错误，不暴露物理路径

#### Scenario: 并发写入结果

- **WHEN** 旧 attempt 和新 attempt 同时写入同一逻辑 task 的结果目录
- **THEN** 只有与当前 attempt、claim 和 workspace 绑定且完整校验的结果可被采纳，旧 attempt 不能覆盖新结果

### Requirement: 旧入口和公开协议必须保持兼容

重构 MUST 保留现有模块的公开 import、任务状态字段、task/job 标识、workspace selector、executor HTTP 字段和 service DNS；兼容入口只能委托新职责，不能维护第二份状态事实。

#### Scenario: 旧 import 继续工作

- **WHEN** 现有 API、脚本或扩展从旧模块导入 Task/Worker/Runner/Executor facade
- **THEN** 导入成功并使用新职责实现，返回字段与既有协议兼容

#### Scenario: 旧任务恢复

- **WHEN** 服务重启后读取旧版本任务记录或旧 attempt 元数据
- **THEN** 可验证的任务继续按原 task 事实恢复，不可验证记录以稳定错误收束，不被静默删除或伪造成功
