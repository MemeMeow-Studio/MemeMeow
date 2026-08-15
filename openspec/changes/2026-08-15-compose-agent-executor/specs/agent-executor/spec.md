## Purpose

定义运行在 Agent 容器内、供 API 通过 Compose 内部网络调用的固定 OpenCode executor。

## ADDED Requirements

### Requirement: executor 必须使用非空认证并提供真实健康检查

executor MUST 要求非空 Bearer token；token MUST 由非 root executor 首次启动时在
独立 named volume 中原子生成，文件权限 MUST 为 0600，后续启动复用该文件，API
只读同一文件。健康检查 MUST 验证 executor 进程、OpenCode 可执行文件、
`/runtime` 可写、图片和 Skill 只读以及 Docker socket 缺失。

#### Scenario: token 文件首次初始化

- **WHEN** named volume 中不存在 token 文件且 Compose 以默认配置启动
- **THEN** 非 root executor 原子生成随机非空 token，文件权限为 0600，API 在 executor 健康后读取同一 token

#### Scenario: token 文件不可用

- **WHEN** token 文件不存在、为空、不是普通文件或权限允许 group/other 读取
- **THEN** executor 健康检查失败，API 不应接收 Agent 任务

#### Scenario: executor 崩溃

- **WHEN** executor 进程退出
- **THEN** Compose 按 restart 策略重启服务，API 依赖的健康条件在恢复前保持未满足

### Requirement: executor 只能接受固定结构化任务

executor MUST 只接受任务 ID、图片相对路径、反向图片策略、超时和等待标志等预定义
字段。executor MUST 拒绝未知字段、任意 shell/command/prompt/env/workdir、绝对图片
路径、`..` 跳转、符号链接和受控图片根目录外的文件。

#### Scenario: 任意 shell 请求

- **WHEN** API 请求包含 `command`、`shell` 或任意环境变量字段
- **THEN** executor 返回 `invalid_task`，且不启动子进程

#### Scenario: 路径越界

- **WHEN** 任务图片路径是绝对路径、包含 `..` 或解析后逃出 `/images`
- **THEN** executor 返回 `agent_image_path_forbidden`

### Requirement: 任务必须支持状态、等待、超时和取消

executor MUST 支持同步等待和异步状态查询；任务 MUST 受并发上限和队列背压约束。
超时或取消 MUST 只终止对应 OpenCode 进程组，不得停止 executor 或其他任务，并返回
稳定的 `agent_timeout` 或 `task_interrupted` 错误。

#### Scenario: 取消运行中的任务

- **WHEN** 调用方取消一个 running 任务
- **THEN** 该任务进入 cancelled/`task_interrupted`，其他任务和 HTTP 服务继续可用

#### Scenario: 队列超过上限

- **WHEN** 排队任务数达到配置的背压上限
- **THEN** 新任务返回 429 和 `agent_backpressure`，不绕过并发限制

### Requirement: 结果必须沿用任务专属文件协议

executor MUST 为每个任务使用独立 `/runtime/task-results/<task_id>/`，只接受固定的
`result.json.tmp` 最终文件，并限制文件大小和基本 JSON 字段。后端 MUST 继续执行
完整 JSON Schema、业务字段、图片指纹和数据库写回校验。

#### Scenario: 成功结果

- **WHEN** OpenCode 原子 rename 出合法结果文件
- **THEN** executor 返回 session ID 和成功状态，后端读取同一任务文件并提交业务结果

#### Scenario: 缺失或超限结果

- **WHEN** OpenCode 未生成最终文件或文件超过限制
- **THEN** 任务失败并返回 `agent_result_file_missing` 或 `agent_result_file_too_large`
