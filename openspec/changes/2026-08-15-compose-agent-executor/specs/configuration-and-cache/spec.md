## MODIFIED Requirements

### Requirement: Compose 内部连接和认证配置必须明确

Compose MUST 使用服务 DNS 配置 Agent executor 和视觉服务地址，不得把生产内部调用
默认到 `127.0.0.1` 或 `host.docker.internal`。executor token MUST 非空，并由非 root
executor 在独立 named volume 中以 0600 权限生成和持久化，API 只读该文件；token 不得
写入日志、结果文件、OpenCode 子进程环境或 API 响应。

#### Scenario: 初始化内部 token

- **WHEN** 干净宿主 shell 运行标准 Compose 启动路径
- **THEN** executor 在首次启动时生成随机非空 token，API 读取同一 token，不依赖宿主环境变量或提交的密钥

### Requirement: Agent 运行模式必须明确且失败关闭

Agent 运行模式 MUST 仅接受 `auto`、`executor` 和 `host`。`auto` MUST 在 executor URL
与 token 均可用时选择 executor，否则选择 host；显式 `executor` 缺少任一必要配置时
MUST 返回稳定配置错误，不得回退 host。配置 MUST NOT 再接受 `docker` mode、Agent
容器名或容器运行时设置。

#### Scenario: 显式 executor 配置不完整

- **WHEN** 运行模式为 `executor`，但 executor URL 或 token 缺失
- **THEN** API 启动或提交 Agent 任务时失败关闭并返回稳定配置错误，不在宿主启动 OpenCode

#### Scenario: 本地 auto 未配置 executor

- **WHEN** 非 Compose 本地环境使用 `auto` 且未配置 executor URL 和 token
- **THEN** 系统沿用 host 执行路径，不探测 Docker、容器名或容器运行时

#### Scenario: 旧 Docker 配置仍被提供

- **WHEN** 配置把运行模式设为 `docker` 或仅提供旧 Agent 容器名/容器运行时字段
- **THEN** 系统拒绝不支持的运行模式，且旧字段不能启用任何容器执行分支

### Requirement: 后端视觉可用性必须读取 Compose 视觉健康状态

当视觉服务运行在独立 Compose 容器时，API MUST 从内部视觉健康 URL 和模型身份判断
可用性，不得使用 API 容器不可见的宿主权重相对路径作唯一判断。

#### Scenario: API 容器没有宿主权重路径

- **WHEN** API 容器收到视觉健康查询
- **THEN** 它使用 `mememeow-visual` 的健康配置返回可用性，不误报内部服务状态
