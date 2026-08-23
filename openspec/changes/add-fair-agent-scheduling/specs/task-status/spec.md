## MODIFIED Requirements

### Requirement: 并行长任务必须受公平调度和背压保护
任务服务 MUST 对可并行的长任务施加显式全局并发上限、scope 级运行上限和排队背压，MUST 保证单一 scope 或任务类型不会无限占用所有执行资源，且 MUST 保持活动任务去重和 `queued -> running -> succeeded/failed` 状态语义。Agent lane 的跨 scope 选择 MUST 遵守持久化公平调度协议；其它任务类型可以继续使用各自的 lane 策略。

#### Scenario: Agent 任务不得阻塞其他任务类型
- **WHEN** 语境生成 job 数量超过 Agent 并发上限，且存在 cache generation 或 metadata repair job
- **THEN** 超出上限的语境 job 保持 `queued`，其他任务类型仍能获得其保留的执行资源并更新状态

#### Scenario: 单一 scope 不得占满 Agent lane
- **WHEN** scope A 持续提交大量 Agent 任务，scope B 也有可执行 Agent 任务，且全局 lane 存在可用 slot
- **THEN** scope A 的运行任务数量不超过配置的 scope 上限，调度器按公平顺序给 scope B 分配机会

#### Scenario: 并行任务查询保持一致
- **WHEN** 多个 job 同时更新进度、失败或成功状态，客户端查询任务详情或列表
- **THEN** 每条任务记录只呈现自身的合法状态转换和结果，不出现跨任务覆盖、重复终态或丢失错误

#### Scenario: 服务重启收束并行执行
- **WHEN** 服务在多个语境子进程运行期间重启或关闭
- **THEN** 所有无法证明仍受管理的运行任务都被标记为 `failed/task_interrupted`，公平状态和 lane slot 不遗留不可回收占用，并且不得因旧进程残留而重复消费同一活动任务

### Requirement: 任务认领和去重必须支持并发进程
系统 MUST 使用数据库原子操作确保一个任务在任一时刻最多由一个有效 Worker 执行，并 MUST 在并发提交语义相同的活动任务时复用同一 `task_id`。每次认领 MUST 产生递增的 claim generation，所有进度、终态和业务副作用提交都 MUST 验证当前 claim；租约过期的旧 Worker 不得写回。Agent lane 的全局并发上限、scope 级运行上限、等待队列背压和公平状态 MUST 对所有应用进程共同生效。

#### Scenario: 两个 Worker 同时认领
- **WHEN** 多个 Worker 同时尝试认领同一排队任务
- **THEN** 只有一个 Worker 获得有效租约并执行该任务，且公平状态只为成功 claim 推进一次

#### Scenario: 过期 Worker 恢复执行
- **WHEN** 旧 Worker 的租约已过期且任务已被新 Worker 重新认领，旧 Worker 随后恢复并尝试写回
- **THEN** 系统依据 claim generation 拒绝旧 Worker 的进度、终态、业务副作用和公平状态更新

#### Scenario: 并发提交同一图片语境任务
- **WHEN** 多个请求为同一 scope、同一 Meme 指纹同时提交活动语境任务
- **THEN** 系统只保留一个活动任务并向所有提交者返回同一 `task_id`，不会重复消耗该 scope 的运行或排队额度

#### Scenario: Agent 队列达到上限
- **WHEN** 所有进程合计的 Agent 运行任务或排队任务达到对应全局或 scope 配置上限
- **THEN** 新任务被明确拒绝或返回现有去重任务，不因增加应用进程或绕过公平 claim 而突破限制
