## Purpose

定义 OpenCode 在共享 session 数据库下选择受控 workspace、限制常规文件工具访问并保持任务恢复绑定的通用契约，使单用户 local 模式和外部 scope 适配层可以复用同一执行核心。

## ADDED Requirements

### Requirement: OpenCode workspace 必须从可信上下文选择
系统 MUST 通过 workspace provider 把可信任务上下文解析为稳定的 opaque selector 和受控 workspace；普通客户端 MUST NOT 提交或覆盖原始 scope、selector、绝对路径、父目录跳转或任意 workdir。provider 未能解析、selector 非法、目录逃出配置根、路径包含符号链接或可信上下文与 selector 不一致时，系统 MUST 在启动 OpenCode 前 fail-closed。provider/宿主适配层 MUST 保证获准的图片、metadata 和 Skill 视图从根到内容不含符号链接或其它特殊节点，并在视图生命周期内维护该不变量；核心不要求对整个输入树递归扫描。

#### Scenario: 两个可信 scope 选择不同 workspace
- **WHEN** 两个任务的可信上下文解析为不同 opaque selector
- **THEN** 两次 OpenCode 执行使用不同的 `--dir`、配置目录和工作目录

#### Scenario: 客户端尝试覆盖 workspace
- **WHEN** 普通请求或任务 payload 提交 scope、selector、绝对路径、`..` 或 workdir
- **THEN** 系统忽略或拒绝该值，只允许 workspace provider 使用可信任务事实选择目录

#### Scenario: selector 无法安全解析
- **WHEN** provider 返回未知 selector、非法标识、符号链接路径或配置根之外的目录
- **THEN** 系统返回稳定配置错误且不启动 OpenCode 子进程

### Requirement: 所有 workspace 必须复用同一个 OpenCode DB
系统 MUST 为同一运行时中的所有 workspace 设置完全相同的 `OPENCODE_DB`，同时为每个 workspace 分别设置 `--dir`、`cwd`、`OPENCODE_CONFIG` 和 `OPENCODE_CONFIG_DIR`。系统 MUST NOT 为每个 scope 或每个 Task 创建独立 OpenCode DB；共享 DB 不得使一个执行回退到其它 workspace。

#### Scenario: 两个 workspace 启动 OpenCode
- **WHEN** 两个可信任务分别选择 workspace A 和 workspace B
- **THEN** 两个进程的 `OPENCODE_DB` 路径相同，而 `--dir`、`cwd`、`OPENCODE_CONFIG` 和 `OPENCODE_CONFIG_DIR` 均不同

#### Scenario: workspace 配置缺失
- **WHEN** 当前 workspace 的目录或配置无法安全创建和验证
- **THEN** 系统保持共享 DB 不变并拒绝该次执行，不回退到 local 或其它 workspace

### Requirement: workspace 必须限制 OpenCode 常规文件工具
系统 MUST 为选定 workspace 生成不含密钥的 `opencode.json`。`permission.external_directory` MUST 使用对象规则，并按 OpenCode“最后匹配规则生效”的顺序先配置 `"*": "deny"`，再逐项允许服务端装配的只读 Skill、当前 workspace 的只读图片/metadata、当前 Task 临时目录和既有 `task-results/<task-id>` 中两个受控结果文件；具体容器路径必须由 provider 验证后生成，不能来自客户端。OpenCode 的 read、write、edit、glob、grep 等常规文件工具 MUST 能访问获准视图并拒绝其它 workspace、应用 runtime 和宿主目录。图片与 metadata 视图 MUST NOT 被 Agent 改写，只有受控配置、当前 Task 临时目录和两个结果文件 MAY 可写；现有 Task 最终结果协议 MUST 保持独立 Task 路径、原子交付和后端二次校验。provider 的无符号链接不变量是该边界的一部分；若获准根内部仍存在未被 provider 发现的符号链接，`external_directory` 可能跟随它，不能声称 OpenCode 自身阻止该读穿。该配置 MUST NOT 被描述为操作系统沙箱，且本 capability MUST NOT 仅为实现目录权限而禁用既有 Bash、Python、Node 或网络研究能力。

#### Scenario: 读取当前 workspace 的研究输入
- **WHEN** OpenCode 常规文件工具读取 provider 为当前 workspace 提供的图片或 metadata
- **THEN** 读取成功，且不需要把整个应用数据根作为当前工作目录

#### Scenario: 当前任务写入结果
- **WHEN** Agent 写入当前 Task 的临时研究数据和最终结果
- **THEN** 临时数据只能进入当前 Task 可写目录，最终结果继续使用既有 task-results 文件协议并接受原子交付、大小和后端校验

#### Scenario: Agent 改写图片或 metadata
- **WHEN** Agent 尝试写入当前 workspace 的图片、metadata 或只读 Skill 视图
- **THEN** 文件权限和 OpenCode 配置拒绝修改

#### Scenario: 常规文件工具跨 workspace 访问
- **WHEN** OpenCode 常规文件工具使用绝对路径、父目录或 glob 尝试访问其它 workspace，或尝试穿过 provider 已拒绝的符号链接节点
- **THEN** `external_directory` 权限拒绝访问；获准根内部遗漏的符号链接读穿由 provider invariant 和额外 OS 沙箱负责，不由该权限规则承诺

#### Scenario: 宿主需要严格文件隔离
- **WHEN** 宿主要求连 Bash、Python 或 Node 子进程也不能访问其它目录
- **THEN** 宿主必须另行提供 OS 或容器级沙箱，不能把本 capability 的文件工具权限当作该保证

### Requirement: session 恢复必须绑定原 workspace
系统 MUST 在首次执行产生 session 后持久记录 session、业务 Task/attempt 与 opaque workspace selector 的绑定。重试和 resume MUST 从可信持久任务事实恢复 selector，并在传递 `--session` 前验证绑定完全一致；缺失绑定、selector 变化、跨 workspace session 或调用方直接提交的 session MUST 被拒绝，且不得启动恢复进程。该绑定事实 MUST 与 attempt 创建原子提交；executor 若无法在重启后验证绑定 capability，MUST fail-closed。

#### Scenario: 同一 Task 在原 workspace 恢复
- **WHEN** 重试从可信 Task/attempt 元数据取得 session 和与首次执行相同的 selector
- **THEN** 系统在原 workspace 中使用共享 DB 恢复该 session

#### Scenario: session 与 workspace 不一致
- **WHEN** 当前可信 Task 的 selector 与 session 记录的 selector 不同
- **THEN** 系统返回稳定绑定错误且不把该 session 传给 OpenCode

#### Scenario: 客户端直接提交 session
- **WHEN** 普通客户端提交任意 session id 或试图修改 Task 的 session/workspace 绑定
- **THEN** 系统不把该值作为恢复依据，只信任持久 Task/attempt 元数据

### Requirement: executor 只能接受带绑定的 opaque workspace capability
通过独立 executor 运行时，受信 API MAY 在固定结构化任务中传递 workspace provider 生成的 opaque selector 以及由 provider/API 签发的 workspace capability。capability MUST 绑定业务 Task、executor attempt、selector、受众和过期时间，并由 executor 使用配置的验证密钥或等价可验证材料校验。executor MUST 拒绝未知字段、原始 scope、绝对路径、父目录、任意 workdir、未知 selector、过期/错误签名 capability 和不符合受控目录布局的 selector；它 MUST 把 selector 解析到配置的 workspace 根下，并在首次执行和 resume attempt 中保持相同绑定。executor 重启后无法恢复或验证该绑定时 MUST fail-closed，不得仅凭 Bearer token 和请求中的 selector继续执行。

#### Scenario: executor 接受已知 selector
- **WHEN** 受信 API 提交合法 opaque selector、匹配 Task/attempt 的未过期 capability 且对应受控 workspace 已装配
- **THEN** executor 在该 workspace 启动任务，并继续使用运行时共享的 `OPENCODE_DB`

#### Scenario: executor 解析图片相对路径
- **WHEN** 合法任务携带 `image_relative_path`
- **THEN** executor 只在当前 capability/selector 对应的只读图片根解析该路径，不允许它选择全局或其它 workspace 图片根

#### Scenario: executor 收到路径或原始 scope
- **WHEN** 请求包含绝对路径、`..`、workdir、原始 scope、未知 selector 或无法验证的 capability
- **THEN** executor 返回稳定 `invalid_task` 或 workspace 错误且不启动子进程

#### Scenario: executor 恢复到不同 selector
- **WHEN** resume 请求的 selector 或 capability 与源 attempt 记录的 selector/绑定不一致
- **THEN** executor 拒绝恢复且保留源 attempt 事实不变

#### Scenario: 外部 workspace 发现 Skill
- **WHEN** executor 在非 local workspace 启动研究任务
- **THEN** 当前 workspace 能读取受控只读 Skill 视图，且 Skill 路径不由客户端或 prompt 配置

### Requirement: local 模式必须保持既有 workspace 和历史兼容
只有应用显式安装并选择 local provider（例如明确的 local scope resolver）时，系统 MUST 把任务稳定映射到既有 `<runtime>/workspace`，继续复用既有 `<runtime>/opencode.db`、配置、TUI session 历史、结果协议和单用户行为。非 local 上下文缺少 provider 时 MUST fail-closed，不得静默回退 local。升级 MUST NOT 自动搬迁、复制或删除既有 DB 与 workspace。

#### Scenario: 升级现有 local 部署
- **WHEN** 现有部署明确安装 local provider 并升级到新版本
- **THEN** OpenCode 仍从原 workspace 和 DB 启动，既有 session 可继续列出和按可信任务元数据恢复

#### Scenario: local 模式新建任务
- **WHEN** 明确选择 local provider 的单用户模式创建新的图片研究任务
- **THEN** 显式 local provider 返回 `local` 语义并保持既有路径、并发和结果行为

### Requirement: 共享 session 元数据不得通过普通 scope API 泄露
共享 `OPENCODE_DB` 中的 session 元数据 MAY 由受信本地 TUI 或运维诊断使用，但普通 scope API、Task 状态和公开结果 MUST 只返回当前可信 Task/workspace 绑定的 session 摘要，不得枚举、搜索或泄露其它 workspace 的 session id、标题、提示词、路径或消息内容。

#### Scenario: 普通 scope 查询任务 session
- **WHEN** scope-bound API 查询当前 Task 的状态或诊断
- **THEN** 响应最多返回当前 Task 已绑定的 session 摘要，不返回其它 workspace session 列表

#### Scenario: 受信本地 TUI 查看 sessions
- **WHEN** 明确受信的 local TUI 运行在本地 provider workspace 中查看 session 列表
- **THEN** TUI 可使用现有共享 DB 历史，但该能力不通过普通 scope API 暴露
