## ADDED Requirements

### Requirement: 任务公开结果和恢复历史必须使用安全投影

任务状态接口 MUST 按任务类型返回有限结果字段和稳定错误字段，不得返回内部 payload、scope、宿主路径、凭据、完整执行绑定或未经清理的历史消息。恢复历史只可包含经过格式校验的公开错误码、有限消息、attempt 标识和 ISO 时间；缺少有效 session 与 executor attempt 的记录 MUST 报告为不可恢复。

#### Scenario: 任务结果包含任务类型之外的字段
- **WHEN** 任务持久化结果包含模型配置、文件路径或内部执行信息
- **THEN** 任务响应只返回该任务类型的允许字段，其他字段被省略

#### Scenario: 历史恢复绑定不完整
- **WHEN** 旧任务声明可恢复但缺少合法 session 或 executor attempt
- **THEN** 响应将恢复能力收窄为不可恢复，并不暴露损坏的绑定值

#### Scenario: 历史错误消息包含敏感信息
- **WHEN** 任务错误或恢复历史包含 URL、凭据、控制字符或宿主路径
- **THEN** 响应返回稳定错误码和清理后的单行消息，不返回原始敏感内容
