## ADDED Requirements

### Requirement: 任务列表必须按创建时间稳定分页

系统 MUST 提供只包含当前 scope 的任务列表，支持状态和类型筛选，并按 `created_at` 降序、task ID 降序返回，限制每页最多 100 条。cursor MUST 使用与列表相同的 `(created_at, task_id)` 排序键，仅解释当前 scope 中的记录；状态、进度或错误更新不得改变已有任务的排序位置。无效或其他 scope cursor 不得扩大查询范围。

#### Scenario: 新任务位于列表顶部
- **WHEN** 当前 scope 创建一个新任务并重新查询未筛选的任务列表
- **THEN** 新任务按创建时间位于已有任务之前，旧任务的相对顺序保持不变

#### Scenario: 更新任务不改变列表位置
- **WHEN** 列表中的任务发生进度、状态或错误更新
- **THEN** 下一次列表查询反映最新字段，但该任务仍按原创建时间和 task ID 排列

#### Scenario: Cursor page has deterministic continuation
- **WHEN** 调用方按状态或类型筛选并使用上一页返回的 cursor
- **THEN** 下一页只返回排序键严格位于 cursor 之后的当前 scope 记录，不重复或跳过同一创建时间下的任务

#### Scenario: Cursor scope is isolated
- **WHEN** 调用方提供不存在或其他 scope 的 task ID 作为 cursor
- **THEN** 系统不扩大查询范围，并按既有无效 cursor 语义返回当前 scope 的安全结果
