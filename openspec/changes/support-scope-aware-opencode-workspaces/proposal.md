## Why

OpenCode 当前把所有任务固定到同一 `<runtime>/workspace`，这使需要按应用 scope 运行的宿主只能复制或侵入核心执行逻辑，也无法在共享 session DB 时稳定约束任务恢复到原 workspace。核心需要一个通用 workspace 选择扩展点，同时保持开源单用户模式和既有 OpenCode 历史兼容。

## What Changes

- 增加 scope-aware OpenCode workspace provider：调用方提交经过信任边界恢复的逻辑 workspace selector，核心将其映射为受控目录；普通客户端不能提交原始 scope、绝对路径或任意 workdir。
- 所有 workspace 继续复用一个 `OPENCODE_DB`，但每次执行分别设置 `--dir`、`cwd`、`OPENCODE_CONFIG` 和 `OPENCODE_CONFIG_DIR`；每张图片仍使用独立 session。
- 对首次执行、重试和 session resume 校验 workspace 绑定，拒绝用共享 DB 中其它 workspace 的 session 启动当前任务。
- 为每个 workspace 生成不含密钥的 `opencode.json`；`permission.external_directory` 先以 `"*": "deny"` 设置默认拒绝，再把服务端装配的 Skill、当前 scope 的只读图片/JSON 和当前 Task 目录列为更靠后的精确 `allow` 规则；宿主可以显式提供这些受控视图。
- 保留 Bash、Python、Node 和网络研究能力；本 change 不把 OpenCode 文件权限描述为操作系统沙箱，也不保证可执行工具无法读取进程本来可见的其它目录。
- 显式安装 local provider 的单用户模式稳定映射到既有 `<runtime>/workspace`，继续复用原 DB、配置、TUI session 历史和当前行为；非 local 上下文缺少 provider 时 fail-closed。
- 扩展 Compose executor 的受控任务参数，使 API 与 executor 传递服务端生成的 opaque workspace selector 和绑定 Task/attempt/selector/过期时间的签名 workspace capability；拒绝原始 scope、路径、未知 selector 和与可信 Task 上下文不一致的 selector。

## Capabilities

### New Capabilities

- `opencode-workspace`: 定义共享 OpenCode DB、受控 workspace 选择、文件工具权限、session 绑定、executor 传递和 local 兼容契约。

### Modified Capabilities

- `meme-search`: 将固定单 workspace 的 OpenCode 运行时改为通过通用 provider 选择 workspace，同时保留每图片独立 session、并发上限、结果校验和写回语义。

## Impact

- `backend/opencode.py`：runtime 布局、workspace provider、环境变量、配置生成、CLI/TUI 启动与 session resume 校验。
- `executor/server.py` 及 executor 客户端：opaque workspace selector 的请求校验和受控目录解析。
- `backend/config.py` 与应用装配：显式 local provider 以及宿主适配扩展点。
- `docker-compose.yml`：共享 DB、受控 workspace 根、只读 Skill/图片视图和 capability 验证密钥的挂载/环境契约，不新增每 scope 数据库或每 Task 容器。
- `tests/test_opencode.py`、`tests/test_agent_executor.py` 及相关配置测试：共享 DB/独立 workspace、路径拒绝、resume 绑定、local 兼容和并发回归。

本 change 只提供通用适配层，不引入账户、计费或部署专属 scope 解析逻辑。宿主负责从可信持久任务事实生成 opaque selector、签发 workspace capability，并负责 scope 图片、metadata 与 Task 目录的实际视图和生命周期。只有显式安装的 local provider 才能使用 local 兼容映射；非 local 上下文缺少 provider 时必须失败。
