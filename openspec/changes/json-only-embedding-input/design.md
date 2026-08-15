## Context

现有 sidecar 元数据已定义 embedding 字段白名单和 `semantic_document` 格式，但 `MetadataService.embedding_record()` 在读取失败、`pending`、`repair_required` 或生成空文本时会把图片文件名作为文本返回。`SearchService` 因而为每张图片生成一个向量，缓存格式为 v3。

项目已有进程内 `TaskManager`，适合缓存生成、metadata repair 和批量 VLM，但服务重启后不会保留任务。VLM 只产生画面描述，无法可靠确认外部引用或当前语用；语境研究则可能包含视觉观察、反向图片检索和网页核验。OpenCode 运行时间与外部费用都显著高于普通后台函数，因此需要用统一持久任务服务取代内存任务与 VLM 描述链。

OpenCode CLI 已提供 `run --format json --file <image>`；其 `export <session-id>` 会将图片附件内联，可能截断 stdout，不能作为业务解析来源。runner 在 run 结束后临时启动同一 runtime 的 loopback headless server，并通过公开 `GET /session/{id}/message` 读取完成 session。项目的 `skills/research-meme-context` 和输出 schema 是语境生成的运行时契约。该 skill 通过安装脚本链接到 `.agents/skills` 与 `.opencode/skills`，两者必须指向同一受版本控制来源。

本设计依赖已完成的 `image-sidecar-metadata` change 所定义的 sidecar 生命周期、字段来源、人工内容保护和原子写入边界。

## Goals / Non-Goals

**Goals:**

- 上传后异步生成语境 JSON，并让调用方持续查询每张图片的独立 job 状态。
- 让缓存生成、metadata repair 和语境生成共用一个可重启查询的持久任务服务。
- 所有 job 复用一个 OpenCode runtime、DB 和依赖安装，但隔离每张图片的模型会话。
- 只把通过传输解析、schema、字段模型和图片指纹校验的最终 JSON 写入 sidecar。
- 为既有未就绪图片提供可重试的批量补齐路径，再以 JSON 白名单字段生成 v4 检索索引。

**Non-Goals:**

- 让 OpenCode 直接编辑图片库、sidecar、应用代码或检索缓存。
- 在 job 执行期间安装 OpenCode、Node.js 包或复制一套新的 `node_modules`。
- 复用同一个 OpenCode session 处理多张图片，或让一张图片的上下文进入另一张图片。
- Agent 成功后自动重命名图片或立即重建整个 embedding 缓存。
- 自动无限重试失败的外部调用；失败 job 由显式重试重新提交。
- 保留 VLM 配置、描述接口、批量标注或同步 VLM 自动命名作为回退链路。

## Decisions

### 1. 使用固定且非临时的 OpenCode runtime

配置增加 OpenCode 可执行文件、模型、超时和 runtime 根目录。默认 runtime 位于应用 `data_root` 下，而不是 `/tmp` 或可被普通缓存清理的目录：

```text
<data_root>/opencode/
  workspace/
    opencode.json
  opencode.db
  logs/<job-id>.jsonl
  worker.lock
<data_root>/tasks/
  <task-id>.json
```

worker 始终在同一个 `workspace` 中启动 OpenCode，并为进程设置固定 `OPENCODE_DB`。workspace 中的 Agent skill 与 `node_modules` 使用相对链接或运维配置指向预先安装的共享来源；启动检查只验证这些路径，不运行 `npm`、`npx`、`bun` 或其他下载命令。项目根目录的 `scripts/install-agent-skills.sh` 负责开发/部署仓库的 Agent 发现链接，runtime 初始化创建等价的 workspace 链接。

runtime 初始化还会在 `workspace/opencode.json` 原子写入受应用管理的通用配置。该文件只定义名为 `mememeow` 的 `@ai-sdk/openai` Responses provider，以及唯一注册的 `gpt-5.6-luna` 能力参数；`baseURL` 和 `apiKey` 分别引用 `{env:MEMEMEOW_OPENCODE_BASE_URL}` 与 `{env:MEMEMEOW_OPENCODE_API_KEY}`，不写入任何部署密钥。应用从 `.env` 读取这两个值并继承给 OpenCode 子进程。模型选择不写死在 JSON，而由 `MEMEMEOW_OPENCODE_MODEL` 经 `--model` 参数传递，研究任务额外固定传入 `--variant max`。后台 worker 与 `scripts/open-opencode.sh` 都设置同一个 `OPENCODE_DB`、`OPENCODE_CONFIG` 和 `OPENCODE_CONFIG_DIR`，并设置 `OPENCODE_DISABLE_PROJECT_CONFIG=1`，因此交互式 `/sessions` 与非交互任务使用同一份隔离 runtime，不会向上合并项目根目录的 `opencode.json`。

OpenCode DB 仅作为 OpenCode session 的内部存储。应用不查询其 SQLite 表，而是通过临时 loopback headless server 的公开 session messages API 读取 session；应用自己的 job 记录单独保存，避免 OpenCode 升级改变内部表结构时丢失业务状态。

替代方案是为每个 job 设置独立 XDG 目录。该方案会重复初始化数据库、配置和依赖，并可能触发重复下载，故拒绝。

### 2. 每张图片独立 session，所有进程单并发

worker 通过参数数组而不是 shell 字符串启动：

```text
opencode run --dir <workspace> --format json --file <image> --model <model> <prompt>
```

每个 job 都创建新 session，不使用 `--continue` 或已有 session 作为模型上下文。session ID 只用于导出本次结果和诊断。进程不启用 `--auto`；专用 OpenCode 权限配置只允许读取目标图片、skill 和必要的研究工具，并把写入限制在 runtime。

应用进程内使用一个 worker；`worker.lock` 再提供跨进程互斥，避免多 worker 部署时同时启动两个语境生成进程。排队顺序使用创建时间和 job ID 作为稳定次级键。

替代方案是依赖 OpenCode SQLite WAL 支持并发。数据库能并发不代表模型费用、反向图片检索和 workspace 写入适合并发，因此拒绝。

### 3. 用统一持久任务服务取代内存 TaskManager

每条长任务使用独立 JSON 文件保存 `task_id`、任务类型、可序列化输入、状态、时间、尝试次数、结果、错误和有限消息。语境生成输入和结果额外保存图片相对路径、提交时 SHA-256、OpenCode session ID、模型、skill 内容哈希与 sidecar 元数据哈希。写入使用同目录临时文件、fsync 和原子替换；单文件记录避免为该功能引入业务数据库。

任务服务以 task type 到 handler 的注册表替代当前提交 Python closure 的模式，使 `cache_generation`、`metadata_repair` 和 `meme_context_generation` 都能在读取持久 payload 后重建执行。状态仍使用 `queued -> running -> succeeded/failed`，解析、校验和写回阶段通过 `message` 表示。去重键由任务类型和规范化 payload 决定：缓存生成复用活动同类任务，语境生成按相对路径与 SHA-256 复用活动任务。

启动时扫描 task 文件：终态原样保留，`queued` 重新进入内存队列，`running` 因无法证明旧执行状态而改为 `failed/task_interrupted`。关闭服务时终止受管理的子进程组并持久记录同一错误。`GET /tasks/{id}` 只读取这一任务服务，保持既有响应结构；新增 `GET /tasks` 从相同存储按 `updated_at` 和 task ID 稳定倒序扫描，接受状态、类型、cursor 和 limit，并只返回任务列表所需的受限摘要。

替代方案是保留 `TaskManager` 并新增 Agent job store。该方案会让同一个 `/tasks` 接口背后存在两套状态、互斥和重启语义，也会在移除 VLM 后继续保留不必要的任务抽象，故拒绝。

### 4. 上传自动排队，批量补齐只处理未就绪图片

上传流程先完成图片校验、图片落盘和 `pending` sidecar，再创建 `meme_context_generation` 任务，并把 `metadata_job_id` 写入该文件的上传结果。OpenCode 未安装或模型未配置不回滚有效上传；任务在执行前检查失败并保留诊断。上传请求中的 `auto_name` 作为可序列化任务输入保存，不再同步调用 VLM。

显式单图入口允许用户重试失败或刷新非人工字段。批量入口扫描缺失、`pending`、`partial` 和 `repair_required` 记录，先通过现有 metadata repair 建立可校验的基础 sidecar，再逐图创建或复用 job。人工来源字段仍由 `MetadataService.update_context` 的既有保护规则决定是否可覆盖。

成功写回只使当前检索缓存失效。若任务输入的 `auto_name=true` 且写回了非空 title，任务在最终图片 SHA-256 复核后调用既有安全重命名逻辑，同步移动图片与 sidecar；冲突或目标变化只记录命名失败，不撤销已验证的语境写回。多个任务完成后由用户或上层批次显式触发一次缓存生成，避免每张图片都重新 embedding 整个库。

### 5. 把 JSONL 事件流与业务 JSON 分层解析

runner 将 `opencode run --format json` 的 stdout/stderr 流式写入本次运行的临时文件，逐行解析 stdout 以取得 session ID 和阶段信息，不设置 CLI 输出总字节门禁。runtime 只保留有限诊断前缀，避免完整 transcript 长期占用磁盘；超时、非零退出或事件行不是合法 JSON 时，终止整个子进程组并返回稳定错误。

OpenCode 正常退出后，runner 临时启动只监听 `127.0.0.1` 的 headless server，通过公开 `GET /session/{id}/message` 将完成 session 流式落入临时文件后解析，不设置响应总字节门禁。它选择最后一条完整 assistant 消息，只合并其中的 text parts。它不从工具结果、搜索摘要、thinking 或中间 assistant 消息提取候选，也不直接查询 OpenCode DB。

最终文本先尝试作为完整 JSON 解析；失败时只允许存在唯一一个标记为 JSON 的 fenced block。多个代码块、额外说明或用“第一个左花括号到最后一个右花括号”猜测边界都被拒绝。这样可防止网页内容、工具输出或模型解释被错误当成 sidecar 数据。

### 6. 通过四层提交门槛写回 sidecar

候选数据依次通过：

1. `skills/research-meme-context/references/output-schema.json` 的 JSON Schema 与 URI format 校验；
2. `MemeContext` Pydantic 模型的清理、数量、长度和允许字段校验；
3. job 中相对路径、当前文件存在性和图片 SHA-256 校验；
4. `MetadataService.update_context(..., producer="research")` 的字段来源与人工内容保护。

任一步失败都不写候选字段。成功后 sidecar 通过既有临时文件和原子替换提交，job 保存 session ID、sidecar 元数据哈希和简短结果，不保存密钥或完整网页内容。Agent prompt 不提供 canonical sidecar 写入任务；即使模型尝试越界，OpenCode 权限与后端提交门槛仍阻止其成为主数据。

有效研究输出即使保留 `uncertainties` 或 `meaning=null`，也表示本轮研究已完成，可以进入 `ready`；未知事实不因字段为空而被伪造。非空 `title` 默认只写入 sidecar，只有保存于任务输入中的 `auto_name=true` 才允许在写回后触发安全异步重命名。

### 7. 移除 VLM 描述链，不保留双写回来源

移除 VLM 配置、`backend/labeling.py`、单图候选描述、批量 VLM 标注及其前端入口。上传时不再同步等待视觉模型，也不因 VLM 不可用返回候选描述失败。所有自动产生的 `title`、画面摘要、主体、可见文字和关键词都来自经本设计第 5、6 节校验后的 Agent 输出；研究流程已有的视觉观察阶段仍可使用模型能力，但不再暴露为应用自身的 VLM API。

替代方案是保留 VLM 作为 OpenCode Agent 的快速回退。两者会对同一 sidecar 字段产生不同来源和覆盖顺序，既增加配置和测试面，也使用户无法判断哪个结果用于 embedding，故拒绝。

### 8. 元数据服务返回显式的索引资格和跳过原因

`embedding_record` 保留图片指纹与元数据指纹，但在不可索引时返回空语义文本、`indexable=false` 和稳定跳过原因，而不是返回文件名。可索引的前提是 sidecar 校验成功、状态为 `partial` 或 `ready`，且 `semantic_document` 产生非空文本。

候选原因按外部可理解的语义区分：缺少或无效元数据、语义状态不可用、或白名单字段为空。实现可以保留更细粒度的内部错误码，但缓存任务对外聚合为稳定类别。

### 9. v4 缓存只保存当前可索引图片

缓存生成扫描完整图片库，只有 `indexable=true` 的记录才调用 embedding 模型并写入 `items`。缓存加载重新计算当前图片的索引资格，并要求缓存路径集合恰好等于当前可索引集合；每个条目继续校验图片 SHA-256、元数据哈希和语义文本哈希。JSON 修复或 Agent 写回后，旧缓存自动失效并要求重建。

缓存升级为 `search-cache-v4.json`，payload 保存 `indexed_count`、`skipped_count` 和按原因聚合的统计，生成函数把相同统计返回任务结果。若没有可索引图片，生成以 `no_indexable_images` 失败，在写临时文件或替换前终止，已有 v4 内存索引保持可用。

文件相对路径只用于定位、去重和稳定排序，不得进入 `semantic_document`、语义哈希或 embedding 请求 input。

### 10. 任务状态使用独立的 Operate 工作面

移除 VLM 标注页后，侧栏增加“处理任务”入口。该页面服务于需要确认上传、批量补齐、缓存生成和 repair 是否完成的操作者，不承担产品介绍或操作教学；信息密度、稳定排序和重复扫描效率优先于装饰。

桌面端主体是无嵌套卡片的任务表格：状态、任务类型、关联图片、当前阶段/进度、更新时间和结果作为稳定列。顶部将状态筛选与任务类型筛选置于同一工具栏，当前筛选本身表达可查看范围。语境生成行显示受控缩略图与当前或最终文件名；缓存和 repair 行使用任务图标而非伪造图片。点击行打开详情侧栏，展示时间线、有限错误/结果、自动命名结果和失败语境任务的重试按钮。

上传结果中的 `metadata_job_id` 显示为状态化任务入口，直接导航到详情或在任务页定位该行。页面只在可见且存在 `queued`/`running` 行时请求活动任务更新；所有终态行保留为可分页历史，停止轮询。列表加载使用行级 skeleton，空状态直接提供与当前筛选相关的可执行入口，错误状态提供重试查询动作。

移动端保留同一筛选和排序语义，把表格行折叠为带字段标签的分隔列表，详情使用全高 drawer 或原生 dialog，不把操作隐藏在悬停状态。状态同时使用图标、文字和颜色；重试、刷新和关闭等紧凑工具使用熟悉图标并带 tooltip 和可访问名称。

**前端表达规则：**不得在页面区块、筛选栏、表格或详情模块下方添加仅解释功能、快捷键或使用步骤的小号说明文字。使用路径必须由页面层级、清晰控件标签、状态筛选、进度、空/错误状态、禁用状态、行内操作和 tooltip 表达。空状态与错误状态可以使用短标题和直接动作，但不得退化为功能说明段落。

## Risks / Trade-offs

- [Risk] 图片、网页和搜索结果可能包含 prompt injection。→ Agent 权限限制写入范围，后端只接受最后 assistant JSON，并执行 schema、字段来源和目标指纹校验。
- [Risk] OpenCode 子进程超时或服务关闭后成为孤立进程。→ 为每个 job 建立独立进程组，超时和 shutdown 时终止整个进程组，再持久记录失败。
- [Risk] 多应用进程重复消费同一 job。→ 进程内单 worker 加 runtime 文件锁，job 状态更新使用原子替换并在启动前复核。
- [Risk] 统一持久任务服务迁移会影响既有缓存和 repair 任务。→ 保持原有 `task_id`、状态和查询响应字段，使用 handler 夹具覆盖三种任务类型的提交、恢复和失败。
- [Risk] OpenCode 事件格式或内部 DB 随版本变化。→ 只依赖公开 JSON event 和 loopback session API，不查询 DB 表；固定受支持版本并用录制事件夹具测试解析器。
- [Risk] runtime 日志、session 和 job 文件持续增长。→ 完整 stdout/stderr 与 session 响应只在单次运行的临时文件中流式处理，持久日志只保留有限诊断前缀；清理策略按终态保留期单独配置，不删除 sidecar。
- [Risk] 第三方反向图片检索可能带来隐私和费用。→ 仅在服务端明确配置并允许时把图片交给第三方，单并发执行并保留 job 诊断。
- [Risk] 元数据尚未研究的图片会暂时从搜索结果消失。→ 上传自动排队并提供既有图片批量补齐，缓存任务报告所有跳过原因。
- [Risk] 移除 VLM 后，描述和自动命名不再同步返回。→ 上传始终返回 Agent task ID；只在显式 `auto_name=true` 的终态任务中异步改名，前端通过任务状态展示结果。
- [Risk] v3 与 v4 同时留在磁盘。→ v4 不读取 v3；稳定后由独立运维清理，本 change 不做不可逆删除。

## Migration Plan

1. 将 `skills/research-meme-context` 和安装脚本纳入版本控制，在部署环境执行脚本并验证 Codex/OpenCode 两个发现入口。
2. 配置 OpenCode 可执行文件、模型、固定 runtime 和预安装依赖；初始化 workspace 链接和统一 task store，不下载运行时依赖。
3. 用 handler 注册表迁移缓存生成和 metadata repair 到持久任务服务，保持任务查询响应兼容；验证重启后 queued/running 任务的收束语义。
4. 发布 VLM 配置、后端、接口和前端移除，以及 Agent 自动排队、单图重试和批量补齐接口；先用测试图片验证 session 导出、解析、异步自动命名和原子写回。
5. 对既有缺失或未就绪图片提交批量任务，检查失败原因并按需显式重试。
6. 发布 JSON-only 索引和 v4 缓存，完成语境补齐后统一重建；v3 保留用于应用版本回滚。
7. 如需回滚，停止 Agent worker并回退应用版本；图片和已校验 sidecar 仍兼容，v3 缓存仍在，OpenCode DB 与任务日志留待诊断而不参与旧版本运行。
