## 1. 基线与扩展点

- [x] 1.1 核对 `resume-opencode-session-after-failure`、`2026-08-15-compose-agent-executor`、`json-only-embedding-input` 和当前 `meme-search` 的最终实现/测试边界，记录 workspace、attempt、session 和结果目录的唯一 owner，避免复制状态机或覆盖 active change。
- [x] 1.2 定义带中文 docstring 的 workspace provider 协议、不可变解析结果和显式 local provider；结果包含 opaque selector、`--dir`/配置目录、只读图片/metadata/Skill 根、Task 临时根与既有 `task_results_root`，但不向普通 API 模型增加 scope、path、env 或 workdir 字段。只有明确 local 应用装配可以选择 local provider，非 local 环境缺失 provider 时 fail-closed。
- [x] 1.3 实现 selector 字符/长度、配置根 containment、逐级 `lstat`、符号链接和目录类型校验；未知、缺失或不一致 workspace 必须在任何子进程、结果目录或 session 副作用前返回稳定错误。

## 2. OpenCode 运行时参数化

- [x] 2.1 将 OpenCodeRunner 的固定 workspace 改为每次任务只解析一次的 workspace 描述；host CLI、TUI、`cwd`、`--dir`、环境构造和日志/结果路径在同一调用链使用该描述，`OPENCODE_DB` 始终保持 `<runtime>/opencode.db`。
- [x] 2.2 按 workspace 原子生成权限为仅运行用户可写且不含密钥的 `opencode.json`/配置目录，保留 provider/model 模板和 `OPENCODE_DISABLE_PROJECT_CONFIG=1`；`permission.external_directory` 按最后匹配优先语义先写 `"*": "deny"`，再逐项允许 provider 验证的只读 Skill、当前图片/metadata、Task 临时根和结果目录父级检查，另以 `permission.edit` 先拒绝全部编辑后只允许当前 Task 临时根和两个受控结果文件，禁止从客户端或 prompt 生成允许项。
- [x] 2.3 保持显式 local provider 使用既有 `<runtime>/workspace`、`<runtime>/task-results`、共享 DB、TUI 和配置路径；升级不得搬迁、复制或删除既有 runtime/session 数据，非 local 工厂缺 provider 时不得选择该路径。
- [x] 2.4 让外部 provider 模式把 `image_relative_path` 只解析到当前 selector 的只读图片根，并提供只读 metadata/Skill 视图与当前 Task 临时目录；草稿/最终结果继续使用既有 `<runtime>/task-results/<task-id>` 协议，保留文件大小、原子 rename、symlink 防护、retention、attempt 防晚到和后端二次校验。

## 3. Session 与任务恢复绑定

- [x] 3.1 在首次 attempt 的受控持久元数据中原子记录 session id 与 opaque workspace selector，并让成功、可恢复失败和诊断读取返回同一绑定事实；普通 scope API 只返回当前 Task 的绑定摘要，不提供共享 DB session 枚举。
- [x] 3.2 在 host 重试/resume 传递 `--session` 前，从可信 Task/attempt 元数据恢复 workspace 并比较 selector；缺失、客户端提交或不一致绑定返回 `opencode_workspace_mismatch`，不得启动恢复进程或回退 local。
- [x] 3.3 覆盖 Task 目标变化、claim/attempt 轮换、取消和进程未确认收束场景，证明新增 workspace 绑定不放宽既有 session 可恢复条件，也不使旧 attempt 写回当前任务。

## 4. Compose Executor 协议

- [x] 4.1 在 executor 客户端和固定请求 schema 中只增加 `workspace_selector` 与 `workspace_capability`，继续拒绝未知字段、原始 scope、绝对路径、父目录、shell、prompt、env 和 workdir；capability claims 至少包含 `task_id`、`attempt_id`、`workspace_selector`、`audience`、`exp`，resume 时增加 `session_id`、`resume_of_attempt_id`，签发输入只来自可信持久 Task/attempt 事实。
- [x] 4.2 在 executor 内验证独立 verifier 或从 service secret 域分离派生的签名材料，再将 selector 解析到配置 workspace 根的固定布局；重复执行字符、containment、节点类型和 symlink 校验，为选定 workspace 设置共享 `OPENCODE_DB`、独立配置、`cwd`、`--dir`，并保留现有 Task 结果根。
- [x] 4.3 将 selector 写入 executor TaskState/attempt 元数据用于诊断；创建 resume attempt 时验证 capability 中的 session、源 attempt 与 selector，错误或重启后无法验证的请求不得创建新 attempt、目录或 OpenCode 子进程，不能仅凭 executor Bearer token 接受 selector。
- [x] 4.4 保持 executor 全局并发/背压、等待、状态、取消、超时、进程组收束、结果大小和清理协议不变，验证不同 workspace 不会各自获得一套并发上限。
- [x] 4.5 更新 Compose 挂载和环境契约，使 executor 能读取共享 DB、受控 workspace 根、当前 selector 的只读图片/metadata 视图、只读 Skill 和 workspace capability 验证材料；验证 API/executor 容器对同一逻辑图片根的一致映射，同时不新增数据库凭据、Docker socket、项目根或任意宿主目录访问。

## 5. 权限与兼容测试

- [x] 5.1 补充 `tests/test_opencode.py` 与 `tests/test_opencode_workspace.py` 的 OpenCode 运行时/workspace 测试，覆盖两个 workspace 的 DB 路径完全相同而 `--dir`/`cwd`/配置目录不同、同 workspace 重试稳定、非法 selector fail-closed，以及 local 旧路径/TUI/session 历史兼容。
- [x] 5.2 补充 `tests/test_agent_executor.py` 与 `tests/test_opencode_workspace.py` 的 executor 测试，覆盖 opaque selector/capability 允许列表、签名/受众/期限/Task/attempt/session 绑定、路径/scope/workdir 注入、未知或未装配 selector、executor 重启后验证、resume selector 不一致、当前 selector 图片根、既有 Task 结果协议和共享全局队列。
- [ ] 5.3 使用受支持的真实 OpenCode 版本验证当前 workspace 图片/metadata 可读、Skill 可发现、Task 临时和 `task_results_root` 的两个受控结果文件可写而图片/metadata/Skill 及其它结果文件不可写，绝对路径、`..`、glob 等跨 workspace read/write/edit/grep 被对象形式 `external_directory` 规则拒绝；获准输入根内部不含 symlink 是 provider/宿主适配层必须维护的不变量，不能声称 `external_directory` 自身能阻止遗漏 symlink 的读穿，测试与文档也不得声称 Bash/Python/Node 已被隔离。OpenCode 1.18.18 探针已确认外部绝对路径的 read/glob/grep 会拒绝，但允许目录中的 symlink 仍可被 read 跟随；初始配置还暴露了普通编辑工具可写输入根和结果文件首次创建的规则缺口，当前实现已补充 `permission.edit` 只写规则和结果目录父级检查。修复后的模型驱动权限链尚未重新完成，且 symlink 行为不能由 `external_directory` 单独保证，因此本项保持未完成。
- [x] 5.4 增加并发回归：不同 workspace 同时运行时配置、session、图片和结果不串，取消/超时一个任务不影响另一任务，共享 DB 与 slot 锁不产生额外活动进程。已通过不同 selector 并发、共享 DB、独立 cwd/config/image/result、取消/超时隔离和全局 worker 数断言。

## 6. 文档与验收

- [x] 6.1 更新运行时、配置和 Compose 文档，说明共享 DB、provider 信任边界及输入视图无 symlink 不变量、显式 local 行为、签名 workspace capability、opaque selector、只读输入/Skill、既有 Task 结果目录生命周期以及 `external_directory` 不是 OS 沙箱；文档不得包含特定商业部署表述。
- [ ] 6.2 运行格式/静态检查、相关单元测试、真实 OpenCode 权限测试和 Compose executor 集成测试，执行 `openspec validate support-scope-aware-opencode-workspaces --strict` 并记录无法在当前环境完成的门禁。单元、编译、Compose 配置渲染、OpenSpec strict 和 OpenCode `debug config` 已通过；真实模型驱动权限链因 symlink 读穿和修复后尚未复测仍未完成，Compose executor 真实 e2e 也未在完整服务/权重环境中执行。
- [x] 6.3 完成实现后由主代理组织独立对抗性审查，重点检查路径/symlink、selector 注入、session 跨 workspace 恢复、共享 DB 并发、local 迁移和 active change 冲突；修复全部 P1/P2 后再提交边界清晰的开源 commit 供用户审核。已完成本轮路径/symlink、capability、恢复绑定、并发、迁移、Compose 契约和 active change 冲突审查，未发现未修复的 P1/P2；5.3/6.2 的真实权限与 Compose e2e 门禁仍按上文保持未完成。
