## Purpose

在保留 Agent 本地分析和网络研究能力的前提下，将其可写入和可读取的宿主机范围限制为明确挂载的数据目录，避免 Agent session 影响宿主机应用与凭据。

## ADDED Requirements

### Requirement: 共享 Agent 容器必须承载全部研究 session
系统 MUST 使用一个长期运行的共享 Agent 容器执行全部图片语境研究任务。每个任务 MUST 使用独立的 OpenCode session，但不为每个任务创建新的容器。

#### Scenario: 连续执行两个研究任务
- **WHEN** 两个图片语境任务先后被 Worker 执行
- **THEN** 两个任务在同一运行中的 Agent 容器中以不同 session 执行

### Requirement: Agent 容器必须具有明确的宿主机访问边界
系统 MUST 仅向 Agent 容器提供所需的目录挂载：OpenCode runtime 为可写，图片、项目 Skill 和运行时依赖为只读。容器 MUST 不获得项目根目录、用户目录、数据库凭据或 Docker socket 的访问能力。

#### Scenario: Agent 读取输入图片和 Skill
- **WHEN** Agent 执行图片研究
- **THEN** 它可以读取被挂载的图片和 Skill，但不能读取未挂载的宿主机路径

#### Scenario: Agent 尝试访问宿主 Docker
- **WHEN** Agent 在容器内检查 Docker socket
- **THEN** 容器中不存在可用的宿主 Docker socket

### Requirement: Agent 容器必须保留研究所需的通用能力
系统 MUST 在 Agent 镜像中提供 OpenCode、Node 运行时、Python、图像格式识别与转换、OCR、JSON 处理、HTTP 客户端和常见文本处理工具。容器 MUST 允许网络访问和 Bash 工具调用。

#### Scenario: Agent 对小尺寸图片执行 OCR
- **WHEN** Agent 需要识别图片中的小尺寸文字
- **THEN** 它可以在容器中运行图像处理和 OCR 工具，并读取结果

### Requirement: Agent 模型连接配置必须只经运行环境提供
系统 MUST 仅向 Agent 容器传入模型 Base URL 和 API Key 所需的运行环境变量，不得将宿主 `.env` 或其他宿主机凭据目录挂载进容器。

#### Scenario: Agent 启动模型调用
- **WHEN** Agent 容器启动 OpenCode session
- **THEN** OpenCode 可以使用模型连接配置，且容器文件系统中不存在宿主 `.env`
