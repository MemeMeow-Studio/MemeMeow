## ADDED Requirements

### Requirement: Agent 结果文件失败必须使用稳定错误标识
系统 MUST 在语境任务因结果文件缺失、不可读、JSON 无效或 schema 无效而失败时，记录对应的稳定错误标识和面向用户的错误消息；不得将未写入语境的任务标记为成功。

#### Scenario: 缺少结果文件
- **WHEN** Agent 会话结束且任务输出目录中不存在结果文件
- **THEN** 任务状态为 `failed`，错误标识为 `agent_result_file_missing` 或等价稳定标识
