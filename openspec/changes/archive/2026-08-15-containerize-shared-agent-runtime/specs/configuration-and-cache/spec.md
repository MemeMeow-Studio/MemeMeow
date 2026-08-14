## MODIFIED Requirements

### Requirement: 模型配置必须来自服务端环境
系统 MUST 从服务端 `.env` 或等价的进程环境读取嵌入模型和 VLM 的 API Key、Base URL、模型标识及路径配置。前端请求不得修改或持久化这些秘密值。Agent 模型连接所需的 Base URL 与 API Key MUST 仅作为 Agent 容器运行环境传入，不得通过挂载宿主 `.env` 提供。

#### Scenario: 启动时读取配置
- **WHEN** 服务启动且环境变量存在
- **THEN** 系统加载配置并使用对应模型服务

#### Scenario: 必需配置缺失
- **WHEN** 执行依赖某模型的操作但对应配置缺失
- **THEN** 系统返回明确的配置缺失错误，不输出密钥内容

## ADDED Requirements

### Requirement: Agent 运行时必须在可用后接收任务
系统 MUST 在共享 Agent 容器运行、所需只读挂载可用且容器内 OpenCode 可执行时才接收语境生成任务。运行时不可用时，系统 MUST 返回明确且不泄露宿主机路径或凭据的配置/运行时错误。

#### Scenario: Agent 容器未启动
- **WHEN** 客户端提交图片语境生成任务且共享 Agent 容器不可用
- **THEN** 系统不创建不可执行的研究任务，并返回明确运行时不可用错误
