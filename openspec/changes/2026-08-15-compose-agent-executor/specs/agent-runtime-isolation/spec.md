## MODIFIED Requirements

### Requirement: Agent 任务必须通过容器内 executor 执行

生产 Compose 模式 MUST 由 Agent 容器内 executor 启动 OpenCode；API 在生产、兼容
和错误恢复路径中均 MUST NOT 调用 Docker CLI、读取 `/var/run/docker.sock`、挂载
Docker socket、按容器名定位 Agent 或控制容器内进程。Agent 容器 MUST 只挂载
runtime、图片和 Skill，并通过 Compose 内网回调 API；本地回滚 MUST 使用显式 host
模式，不得恢复 API 侧容器执行。

#### Scenario: API 提交研究任务

- **WHEN** API 处理语境任务
- **THEN** 它向 `http://mememeow-agent-runtime:<port>` 发送带 token 的固定 JSON 请求

#### Scenario: Agent 检查 Docker

- **WHEN** executor 容器检查 Docker socket
- **THEN** socket 不存在且健康检查通过该条件

#### Scenario: executor 请求失败

- **WHEN** API 调用 executor 超时、被拒绝或返回运行时错误
- **THEN** API 返回稳定任务错误，不调用本地 Docker exec、容器 inspect 或容器进程终止作为回退

### Requirement: 容器实例名必须由 Compose project 隔离

生产 Compose 中的 Agent 与 Visual service MUST NOT 设置固定 `container_name`；实例名
MUST 由 Compose project 生成。内部调用 MUST 继续使用稳定 service DNS，运维诊断
MUST 使用当前 project 下的 service key，不得依赖全局真实容器名。

#### Scenario: 同机启动两个 Compose project

- **WHEN** 两个 project 同时启动 Agent 与 Visual service
- **THEN** Compose 为各 project 创建互不冲突的容器实例，服务间仍通过既有 service DNS 访问

#### Scenario: 运维人员执行容器内诊断

- **WHEN** 运维脚本检查 Agent 或 Visual service
- **THEN** 脚本通过 `docker compose exec <service>` 定位当前 project 的实例，无需固定容器名或容器名环境变量
