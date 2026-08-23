## Purpose

为用户提供通过自然语言查找本地表情图片的稳定行为契约，并在普通语义检索与可选 LLM 查询增强之间保持简单、可回退的使用体验。
## Requirements
### Requirement: 系统必须执行受缓存保护的语义检索
系统 MUST 使用当前 scope 已激活的数据库向量索引执行查询，并返回不超过请求数量的可访问图片引用。当前 scope 没有可用索引时，系统 MUST 不执行不完整检索，并返回明确的索引未就绪错误。查询不得返回其他 scope 的候选结果。生成索引时 MUST 从 PostgreSQL 中非空的 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、非空 `meaning` 和 `keywords` 按固定格式构造语义文本；`search_queries`、`uncertainties` 和 `source_urls` MUST 不得进入 embedding。

#### Scenario: 缓存就绪时返回结果
- **WHEN** 客户端提交非空查询且当前 scope 的向量索引已激活
- **THEN** 系统按相关性返回最多 `n_results` 个当前 scope 的图片引用，且不返回重复引用

#### Scenario: 缓存未就绪
- **WHEN** 客户端提交查询但当前 scope 尚无已激活索引
- **THEN** 系统返回 `503`，错误标识为 `cache_not_ready`，且不返回部分检索结果

#### Scenario: 索引刷新期间查询
- **WHEN** 当前 scope 已有可用索引且新一代索引正在生成
- **THEN** 系统继续使用已激活索引，直到新索引完整生成并原子切换

#### Scenario: 空查询
- **WHEN** 客户端提交空白或缺失的查询文本
- **THEN** 系统返回 `400`，错误标识为 `invalid_query`

#### Scenario: 数据库语境驱动索引生成
- **WHEN** Meme 具有 `partial` 或 `ready` 的可用语境且系统生成索引
- **THEN** 系统仅使用已填充的白名单事实字段构造语义文本，并保存文档、元数据和图片内容指纹

#### Scenario: 不可用语境不得回退文件名
- **WHEN** Meme 为 `pending`、`repair_required` 或白名单语义文本为空
- **THEN** 系统跳过该 Meme 并报告其索引状态，不使用文件名或不确定内容生成 embedding

#### Scenario: 全部 Meme 都不可索引
- **WHEN** 当前 scope 没有任何具有有效语义文本的 Meme
- **THEN** 索引生成任务明确失败或保持索引未就绪，且不以空 generation 替换已有激活索引

#### Scenario: embedding 维度不匹配
- **WHEN** embedding 服务返回的向量维度不是系统配置的固定维度
- **THEN** 系统拒绝写入并使本次 generation 失败，已有激活索引继续可用

### Requirement: 检索结果必须稳定且可去重
系统 MUST 按向量相关性降序返回当前 scope 的结果；相关性相同的结果 MUST 使用稳定 `meme_id` 作为次级排序键。系统 MUST 去除同一 Meme 的重复引用，并 MUST 在返回前确认 Meme 记录和图片文件仍然可访问。

#### Scenario: 相同查询重复执行
- **WHEN** 已激活索引、Meme 记录和图片文件未发生变化且客户端重复提交相同查询
- **THEN** 返回结果的顺序和内容保持一致

#### Scenario: 结果图片不可访问
- **WHEN** 候选 Meme 已删除、指纹失效或其图片文件不可访问
- **THEN** 系统跳过该候选，继续处理其他候选，不返回失效引用

#### Scenario: 相同分数的候选
- **WHEN** 多个候选具有相同相关性分数
- **THEN** 系统按 `meme_id` 稳定排序，不依赖可变文件路径决定顺序

### Requirement: LLM 增强必须可选且可回退
系统 MUST 默认使用普通语义检索；客户端显式启用 LLM 增强时，系统 MAY 先改写查询。LLM 调用失败、超时或未配置时，系统 MUST 使用原始查询执行普通检索，不得因此使整个搜索请求失败。

#### Scenario: 启用增强且调用成功
- **WHEN** 客户端启用 LLM 增强且增强服务成功返回查询
- **THEN** 系统使用增强后的查询执行语义检索

#### Scenario: 启用增强但调用失败
- **WHEN** 客户端启用 LLM 增强但增强服务失败或不可用
- **THEN** 系统回退到原始查询并按普通检索返回结果

### Requirement: OpenCode 运行时必须复用且隔离图片上下文
系统 MUST 在固定 runtime 中执行已安装的 OpenCode，所有图片 job MUST 复用同一套受控配置和预安装的 Node.js 依赖，且任务执行期间 MUST NOT 调用包管理器或为每个 job 下载依赖。系统 MUST 在 `<runtime>/workspace/opencode.json` 维护不含密钥的 `@ai-sdk/openai` Responses provider 配置，其中只注册 `gpt-5.6-luna`；服务地址和密钥 MUST 分别从 `MEMEMEOW_OPENCODE_BASE_URL`、`MEMEMEOW_OPENCODE_API_KEY` 的环境变量引用，模型 MUST 由 `MEMEMEOW_OPENCODE_MODEL` 经命令行传递，并固定传入 `--variant max` 以使用 `max` 推理强度。每张图片 MUST 使用独立的 OpenCode session，后一张图片不得继承前一张图片的会话内容。系统 MUST 通过 PostgreSQL 持久公平 claim、配置的全局并发上限、scope 运行上限和跨进程 slot 互斥限制同时运行的语境生成子进程；默认全局和 scope 上限 MUST 为 `1`，公平状态不可用时 MUST fail-closed，不得退化为进程内或竞争式调度。

#### Scenario: 默认配置保持单并发
- **WHEN** 未配置 OpenCode 并发上限或显式设置为 `1`
- **THEN** 系统一次只运行一个语境生成子进程，并保持现有稳定排队顺序

#### Scenario: 不同图片在安全上限内并行
- **WHEN** 多个不同图片的语境生成 job 同时处于 `queued`，且运行时已验证支持配置的并发上限
- **THEN** 系统最多同时运行该上限数量的 OpenCode 子进程，每个 job 使用独立 session，任一 job 的模型上下文不得进入另一 job

#### Scenario: 并发资源达到上限
- **WHEN** 活跃语境生成子进程数量已达到配置上限
- **THEN** 后续 job 保持 `queued`，不启动额外子进程、不重复调用外部检索服务，并在资源释放后按稳定顺序继续调度

#### Scenario: 多 scope 任务按持久公平序号轮询
- **WHEN** 多个 scope 都有可执行的 Agent 任务且 lane 存在可用 slot
- **THEN** Worker 按 `task_lane_fairness` 的最久未服务序号轮转 scope；服务重启或多个 Worker 并发不会重置或复制内存 cursor

#### Scenario: 公平状态故障时停止 claim
- **WHEN** `task_lane_fairness` 缺失、不可读或无法与 Task/slot 在同一事务提交
- **THEN** 任务保持 `queued` 并返回稳定 `agent_fairness_unavailable`，不创建额外运行进程

#### Scenario: 同一图片重复提交
- **WHEN** 同一相对路径和图片 SHA-256 已有 `queued` 或 `running` 的语境生成 job
- **THEN** 系统返回现有 job 标识，不创建第二个 job，也不启动第二次 OpenCode 或同键反向图片检索调用

#### Scenario: 并行任务目标发生变化
- **WHEN** 任一并行 job 完成前图片被删除、重命名或内容变化，导致路径或 SHA-256 与提交记录不一致
- **THEN** 该 job 进入 `failed` 并返回 `target_changed`，不得把结果写入其他图片或新内容

#### Scenario: 语境写回后合并索引失效
- **WHEN** 一批并行语境 job 分别成功提交数据库语境
- **THEN** 系统可记录每张 Meme 的索引失效，但不得为每张 Meme 立即重建当前 scope 的 embedding；上层显式触发的索引任务必须基于一致的已提交数据库快照生成结果

### Requirement: Agent 输出必须经过后端解析与校验后写回
系统 MUST 把 OpenCode 事件流、工具输出、搜索结果和最终 assistant 文本视为不可信数据。系统只可通过临时 loopback headless server 的公开 session messages API 从成功完成的 session 取得最后一条完整 assistant 文本，不得使用会内联附件并截断 stdout 的 CLI export；只接受一个原始 JSON 对象或唯一的 JSON fenced block，并在输出 schema、数据库语境字段模型、目标 `meme_id`、scope 和图片 SHA-256 全部校验通过后原子写回。Agent MUST NOT 直接写入 canonical 数据库记录。并行执行不得改变这些校验和每张 Meme 的提交顺序。

#### Scenario: 并行任务分别安全写回
- **WHEN** 两个不同 Meme 的 Agent 输出均通过 schema、数据库语境字段、scope 和目标 SHA-256 校验
- **THEN** 系统分别原子提交对应 Meme 的语境记录，保存各自 session ID 和结果哈希，且任一写回不会覆盖另一 Meme 的结果

#### Scenario: 并行任务中一个输出失败
- **WHEN** 一个 job 超时、输出无效或 schema 校验失败，而其他 job 仍在运行
- **THEN** 失败 job 进入稳定的 `failed` 状态且不写入候选字段，其他 job 可以独立继续并完成
