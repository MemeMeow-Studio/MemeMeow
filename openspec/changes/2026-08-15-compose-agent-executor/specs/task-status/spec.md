## MODIFIED Requirements

### Requirement: Agent executor 失败必须映射稳定任务错误

后端 MUST 将 executor 认证失败、不可用、非法任务、超时、取消、并发背压和结果文件
错误映射到稳定任务错误；失败任务不得写入部分语境或伪装成功。任务结果 MUST 保留
有限的 executor session/产物引用用于诊断，不得保留 token、命令或完整 transcript。

#### Scenario: executor 超时

- **WHEN** executor 在固定超时内未完成 OpenCode
- **THEN** 后端请求取消并把任务标记为 `failed`/`agent_timeout`

#### Scenario: executor 拒绝非法任务

- **WHEN** executor 返回路径或协议校验错误
- **THEN** 后端使用对应稳定错误码结束任务，不尝试本地 Docker exec 回退
