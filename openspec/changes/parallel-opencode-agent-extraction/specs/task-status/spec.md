## ADDED Requirements

### Requirement: 并行长任务必须受公平调度和背压保护
任务服务 MUST 对可并行的长任务施加显式并发上限和排队背压，MUST 保证单一任务类型不会无限占用所有执行资源，且 MUST 保持活动任务去重和 `queued -> running -> succeeded/failed` 状态语义。

#### Scenario: Agent 任务不得阻塞其他任务类型
- **WHEN** 语境生成 job 数量超过 Agent 并发上限，且存在 cache generation 或 metadata repair job
- **THEN** 超出上限的语境 job 保持 `queued`，其他任务类型仍能获得其保留的执行资源并更新状态

#### Scenario: 并行任务查询保持一致
- **WHEN** 多个 job 同时更新进度、失败或成功状态，客户端查询任务详情或列表
- **THEN** 每条任务记录只呈现自身的合法状态转换和结果，不出现跨任务覆盖、重复终态或丢失错误

#### Scenario: 服务重启收束并行执行
- **WHEN** 服务在多个语境子进程运行期间重启或关闭
- **THEN** 所有无法证明仍受管理的运行任务都被标记为 `failed/task_interrupted`，并且不得因旧进程残留而重复消费同一活动任务
