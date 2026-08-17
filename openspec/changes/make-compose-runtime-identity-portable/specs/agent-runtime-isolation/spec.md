## ADDED Requirements

### Requirement: Agent 容器必须以部署运行身份执行
共享 Agent 容器 MUST 使用部署提供的非 root 数值 UID/GID 运行，并且仅在受控存储初始化成功后接收研究任务。Agent 对图片存储的挂载 MUST 保持只读；部署身份变更不得扩大其已有的宿主机挂载范围、凭据访问范围或 Docker 访问能力。

#### Scenario: Agent 以非 root 身份启动
- **WHEN** 部署提供有效的非 root 运行身份且存储初始化成功
- **THEN** Agent 进程的有效 UID 不为 `0`，并可以读取其只读挂载中的受控图片

#### Scenario: 初始化未完成
- **WHEN** 受控存储初始化失败或未完成
- **THEN** Agent 不进入可接收研究任务的健康状态
