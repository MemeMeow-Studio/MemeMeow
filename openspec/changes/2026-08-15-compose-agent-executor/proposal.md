## Why

当前 API 进程通过 Docker CLI 和宿主 Docker socket 启动共享 Agent 容器内的
OpenCode。这样把容器编排权限带入业务进程，且 `sleep infinity` 容器本身没有可
验证的任务协议、状态和取消边界。Agent runtime 的 `/runtime` bind mount 还可能
把镜像内非 root 权限覆盖掉，导致全新 checkout 无法稳定启动。

## What Changes

- 在常驻 Agent 容器内运行受限 executor HTTP 服务，固定监听 Compose 内部端口，提供健康、结构化任务提交、状态和取消接口。
- 后端使用 Compose DNS 和非空 Bearer token 调用 executor，不再调用 Docker CLI、读取 Docker socket 或向 Agent 容器注入任意命令/环境变量。
- executor 自己选择固定 OpenCode `run --auto --format json` 调用，限制图片相对路径、任务 ID、策略、超时、并发、结果大小和运行环境；后端继续读取并校验共享任务结果文件。
- Compose 以 executor 健康检查作为 API 启动条件，API 只绑定 `127.0.0.1:8275`；视觉服务和 executor 端口均不发布到宿主机。
- 用 named volume 共享持久 runtime，保留 session、缓存和结果产物，同时避免 bind mount 覆盖非 root 写权限。
- Compose 为视觉状态注入内部健康 URL，API 不再用宿主模型权重相对路径判断视觉服务可用性。

## Capabilities

### New Capabilities

- `agent-executor`: 定义受认证、固定任务接口的 Agent executor 生命周期、并发、结果和取消语义。

### Modified Capabilities

- `agent-runtime-isolation`: 将 Docker socket/CLI 执行边界替换为容器内 executor，并明确 executor 的挂载和网络边界。
- `configuration-and-cache`: 增加 executor token、Compose DNS 和视觉服务内部健康配置，所有敏感字段继续只在服务端或 executor 环境中存在。
- `task-status`: Agent 任务通过 executor 状态和稳定错误完成，超时、取消、非法路径和结果错误不得伪装成功。

## Impact

影响 `backend/opencode.py`、`backend/agent_executor.py`、`backend/config.py`、
`executor/server.py`、Agent Dockerfile、Compose、视觉健康客户端、测试及运行文档。
现有 OpenCode 结果文件协议、数据库任务契约和宿主模式兼容夹具保留；生产 Compose
路径不再依赖 Docker API 权限。
