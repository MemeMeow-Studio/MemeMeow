## Why

当前 OpenCode Agent 与 API 使用同一宿主机进程权限。Agent 为研究图片需要保留 Bash、OCR、图像处理和网络访问能力，但不应能够破坏宿主机或读取除模型连接信息外的宿主机凭据。

同时，Agent 将结果仅作为会话最终文本返回；当它停在工具调用阶段时，即使研究已完成也可能没有可解析的结果。

## What Changes

- 以一个长期运行的共享 Docker 容器承载全部 OpenCode Agent session，后端通过容器内 OpenCode 执行任务。
- 将图片、项目 Skill 和运行所需依赖按目录只读挂载；仅 `data/opencode` 可持久写入。容器不获得项目根目录、数据库凭据、用户目录或 Docker socket。
- 在镜像中预装 OpenCode Agent 常用的图像、OCR、文本、网络和脚本工具，并保留容器网络访问与 Bash 使用能力。
- Agent 必须先向任务专属临时 JSON 文件写入结果；后端校验 schema 后原子接收，不能再仅依赖会话最后一条文本。
- **BREAKING** 语境生成运行时从宿主机 OpenCode 可执行文件改为共享 Agent 容器；部署必须先初始化并启动该容器。

## Capabilities

### New Capabilities
- `agent-runtime-isolation`: 共享容器运行 OpenCode Agent，并通过挂载边界保护宿主机。
- `agent-result-artifact`: 以任务专属 JSON 临时文件可靠交付 Agent 研究结果。

### Modified Capabilities
- `configuration-and-cache`: Agent 运行时配置和启动诊断增加共享 Docker 容器的可用性要求。
- `task-status`: Agent 未产生可校验结果文件时以明确、可诊断的任务错误结束。

## Impact

影响 `backend/opencode.py`、运行配置与启动脚本，新增 Agent Dockerfile、Compose/容器启动定义和测试。生产部署额外依赖 Docker；前端 API 和 PostgreSQL 数据模型保持不变。
