> **历史关系**：本 change 已被 `introduce-postgres-scoped-persistence` superseded-by。JSON 任务、sidecar 运行时读写和旧缓存格式不再是当前实现契约；其任务与 embedding 语义约束由新 change 接管。归档时先归档新 change，再使用 `--skip-specs` 归档本 change，避免旧 delta 回写主规范。

## Why

JSON-only embedding 可以消除低信息量文件名带来的检索噪声，但如果没有自动生产 sidecar 语义的流程，新上传和既有 `pending` 图片会长期被索引跳过。现有 VLM 描述链与 OpenCode Agent 将产生重复且能力不对等的语义来源；现有内存 `TaskManager` 也无法记录跨重启的长 Agent 任务。需要把“异步生成可信 JSON”“统一持久任务”和“只从 JSON 生成 embedding”合成一条管线。

## What Changes

- **BREAKING**：移除 VLM 配置、单图描述与批量 VLM 标注接口、相关前端流程和同步 VLM 自动命名；OpenCode Agent 成为图片语境与 title 的唯一自动生成者。
- 图片上传成功并创建 `pending` sidecar 后，自动提交独立的 meme 语境生成 job；上传响应立即返回图片结果和 job 标识，不等待 OpenCode 完成。
- 提供显式的单图重试与既有图片批量补齐入口；同一图片内容已有 `queued`/`running` job 时复用原 job，避免重复模型和反向图片检索调用。
- 用统一持久任务服务取代内存 `TaskManager`，让缓存生成、metadata repair 与 OpenCode 语境生成共享任务标识、状态、进度、错误和查询接口；服务重启后可查询历史终态，安全重排尚未运行的任务，并将中断中的任务标记为失败。
- 新增可筛选、分页的任务列表 API 和“处理任务”前端页面，集中展示排队、运行、完成和失败的缓存、repair 与语境生成任务；上传结果直接关联对应任务，失败的语境生成任务可在详情中重试。
- 在固定 runtime 目录中以单 worker 运行已安装的 `opencode`，所有图片 job 复用同一个 OpenCode DB、配置和 `node_modules`，但每张图片使用独立 session；任务执行期间不运行包管理器或重复下载依赖。
- 提供本地 OpenCode 会话检查脚本，复用图片任务的 workspace、DB、配置、模型和 skill，使操作者可通过 `/sessions` 回看历史 Agent 过程；后台 worker 与脚本均隔离项目根目录配置。
- 使用项目内受版本控制的 `skills/research-meme-context` 分析图片；安装脚本只为 `.agents/skills` 和 `.opencode/skills` 创建指向同一来源的相对符号链接。
- OpenCode 只返回候选数据。后端解析 `--format json` 事件流并通过公开 loopback session API 从完成的 session 中取得最后一条 assistant 文本，只接受一个 JSON 对象，再经过输出 schema、Pydantic、目标路径和图片 SHA-256 校验后原子写回 sidecar。
- Agent 输出无效、进程失败、超时或目标图片已变化时，job 进入可诊断的失败状态，不覆盖既有 sidecar；成功写回时记录研究来源并使旧检索缓存失效。
- 非空 Agent `title` 默认只写入 sidecar；仅当上传时显式请求 `auto_name=true`，语境 job 才在 JSON 校验和写回成功后用 title 异步安全重命名图片。每张完成图片都不立即重建整个 embedding 缓存。
- 任务页使用状态筛选、表格行、进度、详情侧栏、空/错误状态和行内操作表达使用方法；不得在模块下方放置解释性小字或功能说明段落。
- 生成 embedding 时只接受 sidecar JSON 中非空 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、`meaning` 和 `keywords` 按固定格式组成的语义文本。
- **BREAKING**：缺少 sidecar、sidecar 无法通过校验、语义状态为 `pending`/`repair_required`，或白名单字段无法组成非空文本的图片不再使用文件名生成 embedding，而是从本次索引中跳过并报告原因。
- 升级缓存格式版本并拒绝载入仍可能包含文件名 embedding 的旧缓存；没有任何可索引图片时生成失败且不发布空缓存。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `meme-search`: 扩展图片语义检索的索引准备流程，使系统异步生成、校验并持久化 meme 语境 JSON，再以 JSON 白名单字段作为图片 embedding 的唯一语义来源。

## Impact

- 主要影响后端配置、上传响应、统一持久任务服务、OpenCode 子进程管理、任务查询、sidecar 写回、缓存生成与版本校验，以及对应的 API 文档、前端和测试。
- 运行环境需要预先安装 OpenCode，并配置模型与固定 runtime；应用复用依赖，不负责在 job 中下载 OpenCode 或 Node.js 包。
- `skills/research-meme-context` 成为部署所需的受版本控制运行时资产；安装脚本和两个 Agent 发现入口必须保持指向同一份内容。
- 依赖 `image-sidecar-metadata` 提供的 sidecar schema、字段来源、原子写入和图片指纹校验；该 change 应先完成同步或归档。
- 现有包含文件名回退向量的 v3 缓存需要重新生成。既有图片可先批量生成语境 JSON，再统一生成 v4 缓存。
