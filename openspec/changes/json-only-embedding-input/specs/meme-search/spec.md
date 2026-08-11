## ADDED Requirements

### Requirement: 系统必须异步生成图片语境 JSON
系统 MUST 在图片上传并成功创建 `pending` sidecar 后，为该图片提交 meme 语境生成 job，并在上传响应中返回 job 标识，不得保持上传请求直到 Agent 完成。系统 MUST 支持显式重试单张图片，并 MUST 支持为既有的缺失、`pending`、`partial` 或 `repair_required` 语境批量提交 job；既有人工确认字段 MUST 继续受保护。

#### Scenario: 上传图片后自动排队
- **WHEN** 客户端上传一张有效图片且图片与基础 sidecar 已持久化
- **THEN** 系统返回成功的图片结果和 `metadata_job_id`，对应 job 初始状态为 `queued`，图片上传不等待 Agent 输出

#### Scenario: 同一图片重复提交
- **WHEN** 同一相对路径和图片 SHA-256 已有 `queued` 或 `running` 的语境生成 job
- **THEN** 系统返回现有 job 标识，不再启动第二次 OpenCode 或反向图片检索调用

#### Scenario: 上传成功但 job 无法记录
- **WHEN** 图片与 `pending` sidecar 已成功保存但系统无法持久化 Agent job
- **THEN** 上传结果保留成功图片并明确报告语境排队失败，图片仍可由后续批量补齐流程处理

#### Scenario: 既有图片批量补齐
- **WHEN** 客户端请求为缺失或未就绪的图片批量生成语境
- **THEN** 系统为每张符合条件的图片返回已创建、已复用或失败的独立 job 结果，不覆盖人工确认字段

### Requirement: 长任务必须使用统一持久记录且可诊断
系统 MUST 使用统一持久任务服务记录缓存生成、metadata repair 和每个语境生成任务，而不是保留仅存在于内存的任务管理器。每条记录 MUST 包含唯一标识、任务类型、序列化输入、`queued/running/succeeded/failed` 状态、创建与完成时间、尝试次数、结果摘要和稳定错误；语境生成任务还 MUST 记录图片相对路径与提交时 SHA-256、OpenCode session ID、模型和 skill 内容哈希。任务查询 MUST 使用既有任务响应结构返回该记录，且 MUST NOT 暴露模型密钥、完整内部提示词或未经截断的工具输出。

#### Scenario: 查询运行中的任务
- **WHEN** 客户端查询正在执行的缓存、repair 或语境生成任务
- **THEN** 系统返回 `running` 状态、当前阶段消息和已知进度，不重复执行任务

#### Scenario: 服务重启后恢复任务记录
- **WHEN** 服务重启且持久记录中存在 `queued` 或 `running` 的长任务
- **THEN** 系统安全地重新排队尚未启动的 `queued` 任务，将无法确认执行状态的 `running` 任务标记为 `failed` 和 `task_interrupted`，并保留终态记录供查询

#### Scenario: OpenCode 未配置
- **WHEN** Agent job 执行时找不到可执行程序、模型配置、共享依赖或 skill
- **THEN** job 进入 `failed` 状态并返回 `opencode_not_configured`，原图片和 sidecar 保持可用

### Requirement: 系统必须提供可筛选的任务列表与详情
系统 MUST 提供任务列表接口，支持按状态和任务类型筛选、按最近更新时间稳定排序并使用 cursor 分页。列表项 MUST 包含任务标识、类型、状态、进度、阶段消息、时间、有限结果摘要及关联图片的受控媒体引用（如有），但 MUST NOT 包含密钥、完整提示词、原始 OpenCode transcript 或未截断日志。既有单任务查询接口 MUST 继续返回完整任务详情。

#### Scenario: 打开任务列表
- **WHEN** 客户端请求不带筛选条件的任务列表
- **THEN** 系统按最近更新时间倒序返回缓存生成、metadata repair 和语境生成任务的统一摘要，并提供下一页 cursor（如有）

#### Scenario: 筛选活跃语境任务
- **WHEN** 客户端按 `queued` 或 `running` 状态及 `meme_context_generation` 类型请求列表
- **THEN** 系统只返回满足全部筛选条件的任务，状态变化后下一次查询反映最新持久记录

#### Scenario: 上传结果关联任务详情
- **WHEN** 图片上传结果含有 `metadata_job_id`
- **THEN** 客户端可使用该标识打开同一任务的详情，不需要从图片文件名猜测关联任务

### Requirement: 前端必须以操作界面呈现任务状态
前端 MUST 提供“处理任务”页面，集中呈现持久任务列表、状态筛选、任务详情和允许的行内操作。活跃任务 MUST 在页面可见时轮询更新，终态任务 MUST 停止轮询。失败的语境生成任务 MUST 提供明确重试操作；缓存和 repair 任务使用其既有显式触发入口。

#### Scenario: 查看任务详情
- **WHEN** 用户选择任务列表中的一行
- **THEN** 前端展示该任务的阶段、进度、时间、有限错误或结果摘要、关联图片和异步自动命名结果（如有），不展示原始模型日志或提示词

#### Scenario: 使用界面表达操作路径
- **WHEN** 用户查看任务列表、筛选栏、详情或上传后的任务结果
- **THEN** 前端通过状态筛选、清晰标签、进度、行内操作、按钮位置、禁用状态、tooltip 和空/错误状态表达可执行操作，不在功能模块下放置仅解释如何使用该模块的小字或说明段落

#### Scenario: 失败任务重试
- **WHEN** 用户在详情中选择失败的语境生成任务重试
- **THEN** 前端提交显式重试请求并展示新建或复用任务的状态，不改变原失败任务的历史记录

### Requirement: 系统必须以 Agent 取代 VLM 自动语境生成
系统 MUST 不再调用或要求 VLM 生成图片描述、候选命名或批量标注。图片自动语境、title 和可供 embedding 的视觉事实 MUST 由完成 schema 校验的 OpenCode Agent 结果生成；系统 MUST 不暴露 VLM 配置、单图描述或批量 VLM 标注接口。

#### Scenario: VLM 配置缺失
- **WHEN** 服务启动或客户端查询运行配置
- **THEN** 系统不读取、返回或要求任何 VLM API Key、Base URL 或模型标识

#### Scenario: 请求已移除的 VLM 接口
- **WHEN** 客户端请求原单图描述或批量 VLM 标注接口
- **THEN** 系统不启动视觉模型任务，并以标准不存在接口响应结束请求

### Requirement: OpenCode 运行时必须复用且隔离图片上下文
系统 MUST 在固定 runtime 中执行已安装的 OpenCode，所有图片 job MUST 复用同一个配置、数据库和 Node.js 依赖安装，且任务执行期间 MUST NOT 调用包管理器或为每个 job 下载依赖。系统 MUST 在 `<runtime>/workspace/opencode.json` 维护不含密钥的 `@ai-sdk/openai` Responses provider 配置，其中只注册 `gpt-5.6-luna`；服务地址和密钥 MUST 分别从 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 的环境变量引用，模型 MUST 由 `MEMEMEOW_OPENCODE_MODEL` 经命令行传递，并固定传入 `--variant max` 以使用 `max` 推理强度。每张图片 MUST 使用独立的 OpenCode session，系统 MUST 限制同一实例最多运行一个语境生成子进程。

#### Scenario: 连续处理多张图片
- **WHEN** worker 依次处理两张不同图片
- **THEN** 两个 job 使用相同 runtime、OpenCode DB 和依赖目录，但拥有不同 session ID，后一张图片不继承前一张图片的会话内容

#### Scenario: 多张图片同时排队
- **WHEN** 多个语境生成 job 同时处于 `queued`
- **THEN** 系统按稳定顺序逐个启动 OpenCode，任一时刻不超过一个运行中的语境生成子进程

#### Scenario: 初始化通用 Provider 配置
- **WHEN** 运行器准备固定 workspace 且 `.env` 已提供 OpenCode 服务地址、密钥和模型
- **THEN** 系统在 workspace 写入不含实际地址和密钥的 `opencode.json`，并以 `--model` 使用配置的模型标识

#### Scenario: 交互检查历史会话
- **WHEN** 操作者运行 `./scripts/open-opencode.sh` 并在 TUI 中执行 `/sessions`
- **THEN** TUI 使用与图片任务相同的 workspace、OpenCode DB、显式配置和 skill，显示可打开的历史 session，且不合并项目根目录的 OpenCode 配置

### Requirement: Agent 输出必须经过后端解析与校验后写回
系统 MUST 把 OpenCode 事件流、工具输出、搜索结果和最终 assistant 文本视为不可信数据。系统只可通过临时 loopback headless server 的公开 session messages API 从成功完成的 session 取得最后一条完整 assistant 文本，不得使用会内联附件并截断 stdout 的 CLI export；只接受一个原始 JSON 对象或唯一的 JSON fenced block，并在输出 schema、sidecar 字段模型、目标相对路径和图片 SHA-256 全部校验通过后原子写回。Agent MUST NOT 直接写入 canonical sidecar。

#### Scenario: 有效 Agent JSON 成功写回
- **WHEN** OpenCode 正常退出且最后一条 assistant 文本包含唯一、有效并对应当前图片的 meme 语境 JSON
- **THEN** 系统以研究来源原子更新 sidecar，将 job 标记为 `succeeded`，保存 session ID 和结果元数据哈希，并使既有检索缓存失效

#### Scenario: Agent 输出不是有效 JSON
- **WHEN** 最终 assistant 文本缺失、包含多个候选对象、带有不允许的额外说明，或无法解析为 JSON
- **THEN** job 进入 `failed` 状态并返回 `agent_output_invalid_json`，既有 sidecar 不被候选内容覆盖

#### Scenario: Agent JSON 不符合 schema
- **WHEN** 候选 JSON 缺少必填字段、类型或长度不合法，或不能通过 sidecar 字段校验
- **THEN** job 进入 `failed` 状态并返回 `agent_output_schema_invalid`，系统保留有限诊断但不写入候选字段

#### Scenario: Agent 运行期间目标变化
- **WHEN** job 完成前图片被删除、重命名或内容变化，导致相对路径或 SHA-256 与提交记录不一致
- **THEN** job 进入 `failed` 状态并返回 `target_changed`，不得把旧分析结果写入其他图片或新内容

#### Scenario: 默认写入 Agent title
- **WHEN** 有效 Agent JSON 包含非空 `title` 且图片上传时未请求 `auto_name=true`
- **THEN** 系统将标题写入 sidecar，不因异步 job 重命名图片，也不立即为该图片重建整个 embedding 缓存

#### Scenario: 显式异步自动命名
- **WHEN** 图片上传时显式请求 `auto_name=true`，且对应 Agent job 已成功写入非空 title
- **THEN** 系统使用 title 安全派生原扩展名的文件名，同步移动图片与 sidecar；目标冲突或图片指纹变化时保留当前路径并报告命名失败

## MODIFIED Requirements

### Requirement: 系统必须执行受缓存保护的语义检索
系统 MUST 使用已生成的图片检索缓存执行查询，并返回不超过请求数量的可访问图片引用。缓存不存在或尚未完成时，系统 MUST 不执行不完整检索，并返回明确的缓存未就绪错误。缓存生成时，图片 embedding 输入 MUST 只来自其 sidecar JSON 中非空的 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、`meaning` 和 `keywords`，并按固定格式组成语义文本；图片文件名 MUST NOT 进入图片语义文本或 embedding。缺少 sidecar、sidecar 无法校验、语义状态为 `pending`/`repair_required`，或白名单字段无法组成非空语义文本的图片 MUST 从本次索引中跳过，而不得使用文件名回退。缓存生成没有任何可索引图片时 MUST 失败并保留既有可用缓存（如有）。

#### Scenario: 缓存就绪时返回结果
- **WHEN** 客户端提交非空查询且缓存已就绪
- **THEN** 系统按相关性返回最多 `n_results` 个图片引用，且不返回重复的图片引用

#### Scenario: 缓存未就绪
- **WHEN** 客户端提交查询但检索缓存不存在或正在生成
- **THEN** 系统返回 `503`，错误标识为 `cache_not_ready`，且不返回部分检索结果

#### Scenario: 空查询
- **WHEN** 客户端提交空白或缺失的查询文本
- **THEN** 系统返回 `400`，错误标识为 `invalid_query`

#### Scenario: JSON 语义文本作为唯一图片输入
- **WHEN** 图片 sidecar 含有一个或多个非空白名单字段且语义状态为 `partial` 或 `ready`
- **THEN** 系统按固定字段顺序构造语义文本并以该文本生成 embedding，且生成的文本不包含图片文件名

#### Scenario: 不可用 JSON 不得回退文件名
- **WHEN** 图片缺少或无法读取 sidecar、状态为 `pending`/`repair_required`，或白名单字段拼接后为空
- **THEN** 系统跳过该图片，不为其调用 embedding 模型，不把文件名作为 embedding 输入，并在缓存生成结果中报告跳过原因

#### Scenario: 全部图片都不可索引
- **WHEN** 客户端触发缓存生成且所有图片都没有可用的 JSON 语义文本
- **THEN** 系统返回稳定的 `no_indexable_images` 失败结果，不发布空缓存，并继续保留原有可用缓存

### Requirement: 检索结果必须稳定且可去重
系统 MUST 按相关性降序返回结果；相关性相同的结果 MUST 使用稳定的图片引用作为次级排序键。系统 MUST 去除同一图片的重复引用，并 MUST 排除无法确认可访问的图片。

#### Scenario: 相同查询重复执行
- **WHEN** 缓存和图片文件未发生变化且客户端重复提交相同查询
- **THEN** 返回结果的顺序和内容保持一致

#### Scenario: 结果图片不可访问
- **WHEN** 候选图片在本地不存在且其远程来源下载失败
- **THEN** 系统跳过该候选图片，继续处理其他候选图片，不返回失效引用

### Requirement: LLM 增强必须可选且可回退
系统 MUST 默认使用普通语义检索；客户端显式启用 LLM 增强时，系统 MAY 先改写查询。LLM 调用失败、超时或未配置时，系统 MUST 使用原始查询执行普通检索，不得因此使整个搜索请求失败。

#### Scenario: 启用增强且调用成功
- **WHEN** 客户端启用 LLM 增强且增强服务成功返回查询
- **THEN** 系统使用增强后的查询执行语义检索

#### Scenario: 启用增强但调用失败
- **WHEN** 客户端启用 LLM 增强但增强服务失败或不可用
- **THEN** 系统回退到原始查询并按普通检索返回结果
