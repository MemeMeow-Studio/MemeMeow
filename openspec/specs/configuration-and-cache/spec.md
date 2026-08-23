## Purpose

集中管理服务运行配置和检索缓存生命周期，保护模型密钥不离开后端，并让用户明确知道何时可以搜索以及何时需要重新生成缓存。
## Requirements
### Requirement: 模型配置必须来自服务端环境
系统 MUST 从服务端 `.env` 或等价的进程环境读取嵌入模型的 API Key、Base URL 和模型标识，以及 OpenCode Agent 的 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY`、`MEMEMEOW_OPENCODE_MODEL` 和受控 runtime 配置。前端请求不得修改或持久化这些秘密值。Agent 模型连接所需的 Base URL 与 API Key MUST 通过 Compose 环境传入 Agent executor，不得通过挂载宿主 `.env` 提供。

#### Scenario: 启动时读取配置
- **WHEN** 服务启动且嵌入模型或 OpenCode Agent 所需环境变量存在
- **THEN** 系统加载相应配置，分别供文本 embedding 服务或 Agent executor 使用

#### Scenario: 必需配置缺失
- **WHEN** 执行依赖 embedding 或 Agent 的操作但对应配置缺失
- **THEN** 系统返回明确的配置缺失错误，不输出密钥内容

### Requirement: 配置查询必须脱敏
系统 MAY 提供运行状态查询，但响应 MUST 只包含模型名称、Base URL 和密钥是否已配置等非秘密信息，绝不得返回完整 API Key。

#### Scenario: 查询配置状态
- **WHEN** 客户端请求配置状态
- **THEN** 响应中的密钥字段仅表示已配置/未配置或脱敏尾缀

### Requirement: 缓存生成必须可观察且互斥
系统 MUST 能够触发当前 scope 的检索向量索引生成，并通过持久任务状态报告其进度和结果。同一 scope 和 embedding 模型存在索引生成任务时，新的生成请求 MUST 返回已有任务或明确冲突，不得并发构建同一代索引；不同 scope 的生成状态不得互相覆盖。新索引只有在完整写入并验证后才能原子激活。

#### Scenario: 首次生成缓存
- **WHEN** 当前 scope 尚无已激活索引且客户端触发生成
- **THEN** 系统创建索引生成任务，任务成功并激活索引后搜索可用

#### Scenario: 已有缓存时刷新
- **WHEN** 当前 scope 已有索引且客户端明确触发刷新
- **THEN** 系统异步生成新一代索引，并继续使用旧的已激活索引，直到新索引完整替换

#### Scenario: 重复触发生成
- **WHEN** 当前 scope 和模型已有索引生成任务处于 `queued` 或 `running`
- **THEN** 系统不创建第二个并发任务，并返回已有任务标识或 `409`

#### Scenario: 新索引生成失败
- **WHEN** embedding 调用、数据库写入或完整性校验失败
- **THEN** 任务进入失败状态，已有激活索引继续可用，未完成的新一代索引不得参与查询

### Requirement: Agent 运行时必须在可用后接收任务
系统 MUST 在共享 Agent 容器运行、所需只读挂载可用且容器内 OpenCode 可执行时才接收语境生成任务。运行时不可用时，系统 MUST 返回明确且不泄露宿主机路径或凭据的配置/运行时错误。

#### Scenario: Agent 容器未启动
- **WHEN** 客户端提交图片语境生成任务且共享 Agent 容器不可用
- **THEN** 系统不创建不可执行的研究任务，并返回明确运行时不可用错误

### Requirement: 视觉模型配置必须由服务端控制
系统 MUST 从服务端配置读取视觉模型标识、权重位置、向量维度、预处理版本和 CPU 推理参数。客户端和 Agent 不得修改这些配置，也不得获得模型权重位置、数据库凭据或可用于调用视觉推理服务的非受控能力。

#### Scenario: 视觉模型配置完整
- **WHEN** 服务启动且视觉模型、权重、维度和预处理配置完整有效
- **THEN** 系统加载对应视觉运行状态，并允许提交视觉向量任务

#### Scenario: 视觉模型配置缺失
- **WHEN** 系统尝试执行视觉向量任务但模型或权重配置不完整
- **THEN** 任务返回稳定的 `visual_model_not_configured` 错误，且不提交后续 Agent 任务

### Requirement: 视觉配置状态必须脱敏且可诊断
配置状态接口 MUST 仅返回视觉能力是否可用、非敏感模型标识、维度和预处理版本。响应 MUST NOT 返回模型权重绝对路径、下载凭据、内部数据库连接或其他敏感配置。

#### Scenario: 查询视觉配置状态
- **WHEN** 客户端请求后端配置状态
- **THEN** 响应包含视觉能力可用性和非敏感模型元数据，但不包含权重路径或凭据

### Requirement: 不同视觉向量空间必须隔离
系统 MUST 以模型标识、维度和预处理版本共同区分视觉向量空间。切换 active 视觉模型时，旧模型向量 MAY 保留用于回滚，但匹配 MUST 只使用当前查询模型对应且维度一致的向量；系统不得平均、拼接或直接比较不同模型空间的向量。

#### Scenario: active 视觉模型切换
- **WHEN** 部署方切换到另一个已配置的视觉模型
- **THEN** 新查询只匹配新模型空间中已就绪的向量，旧模型向量不会与其混合
