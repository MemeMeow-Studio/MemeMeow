## MODIFIED Requirements

### Requirement: Compose 内部连接和认证配置必须明确

Compose MUST 使用服务 DNS 配置 Agent executor 和视觉服务地址，不得把生产内部调用
默认到 `127.0.0.1` 或 `host.docker.internal`。executor token MUST 非空，并由非 root
executor 在独立 named volume 中以 0600 权限生成和持久化，API 只读该文件；token 不得
写入日志、结果文件、OpenCode 子进程环境或 API 响应。

#### Scenario: 初始化内部 token

- **WHEN** 干净宿主 shell 运行标准 Compose 启动路径
- **THEN** executor 在首次启动时生成随机非空 token，API 读取同一 token，不依赖宿主环境变量或提交的密钥

### Requirement: 后端视觉可用性必须读取 Compose 视觉健康状态

当视觉服务运行在独立 Compose 容器时，API MUST 从内部视觉健康 URL 和模型身份判断
可用性，不得使用 API 容器不可见的宿主权重相对路径作唯一判断。

#### Scenario: API 容器没有宿主权重路径

- **WHEN** API 容器收到视觉健康查询
- **THEN** 它使用 `mememeow-visual` 的健康配置返回可用性，不误报内部服务状态
