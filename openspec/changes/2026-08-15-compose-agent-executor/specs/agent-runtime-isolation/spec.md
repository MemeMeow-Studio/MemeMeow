## MODIFIED Requirements

### Requirement: Agent 任务必须通过容器内 executor 执行

生产 Compose 模式 MUST 由 Agent 容器内 executor 启动 OpenCode；API MUST NOT 调用
Docker CLI、读取 `/var/run/docker.sock` 或挂载 Docker socket。Agent 容器 MUST 只挂载
runtime、图片和 Skill，并通过 Compose 内网回调 API。

#### Scenario: API 提交研究任务

- **WHEN** API 处理语境任务
- **THEN** 它向 `http://mememeow-agent-runtime:<port>` 发送带 token 的固定 JSON 请求

#### Scenario: Agent 检查 Docker

- **WHEN** executor 容器检查 Docker socket
- **THEN** socket 不存在且健康检查通过该条件
