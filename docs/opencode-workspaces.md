# OpenCode workspace

OpenCode 在一个运行时内继续共享 `<runtime>/opencode.db`，但每次任务由可信
workspace provider 解析自己的 opaque selector。selector 不是路径，也不是普通
HTTP payload 字段；provider 必须从已 claim 的 Task/scope 事实生成它。API、Worker
和 executor 都会校验 selector、目录 containment、逐级 `lstat` 和符号链接，解析
失败时不会启动 OpenCode 或回退到 local。

外部 provider 的推荐布局是：

```text
<workspace-root>/<selector>/
  workspace/       # OpenCode --dir 和 cwd
  images/          # 当前 scope 的只读图片视图
  metadata/        # 当前 scope 的只读 metadata 视图
  skills/          # 当前 scope 可发现的只读 Skill 视图
```

Compose 模式下 API 在 `/app/data/opencode`、executor 在 `/runtime` 访问同一个
named runtime volume，因此外部 provider 的 selector 目录必须预装配在这份 volume
中；API 侧 workspace 根为 `/app/data/opencode/workspaces`，executor 侧为
`/runtime/workspaces`。API 图片根 `/app/data/images` 与 executor `/images` 是同一
逻辑图片目录，图片和 Skill 挂载为只读。executor 只接受固定的
`MEMEMEOW_EXECUTOR_WORKSPACE_ROOT`、`MEMEMEOW_WORKSPACE_CAPABILITY_KEY` 和共享
OpenCode 配置变量，不读取项目根、Docker socket 或数据库凭据。生产 Compose 仍须
提供非 root 的 `MEMEMEOW_RUNTIME_UID`/`MEMEMEOW_RUNTIME_GID`，并由宿主适配层负责
创建与回收 selector、图片、metadata 和 Skill 视图。

provider 的输入视图有一个硬性不变量：宿主适配层在交给 runner 前必须逐级创建并
保持 `images/`、`metadata/` 和 `skills/` 下的目录、普通文件及其祖先不含符号链接
或其它特殊节点；视图内容变化时也必须由适配层重新校验并拒绝违反该不变量的视图。
核心只校验 workspace 根和当前请求的图片/metadata 路径，不对整个输入树做递归扫描。
因此，已允许根内部若存在由适配层遗漏的符号链接，OpenCode 的
`external_directory` 可能跟随它读取到根外路径；这不是该权限机制能够单独保证的
边界，也不应被描述为 OpenCode 防止了这种读穿。需要对不可信视图或链接跟随提供
强保证时，必须额外使用独立 Unix 用户、mount namespace 或容器级 OS 沙箱。

Task 临时数据和本次任务的 OpenCode 配置位于 `workspace/tasks/<task-id>/`；这样同一
workspace 并发任务的精确权限规则不会互相覆盖。最终草稿和结果仍使用既有的
`<runtime>/task-results/<task-id>/result.json.draft` 与 `result.json.tmp`，后端会
再次校验大小、JSON、schema、SHA 和 claim fencing。不同 selector 使用不同的
`--dir`、`cwd`、配置文件和配置目录，但 `OPENCODE_DB` 必须保持同一个 runtime
路径。

配置中的 `permission.external_directory` 先拒绝 `*`，再允许 provider 验证的
只读输入、Skill、当前 Task 临时目录以及当前结果目录的父级检查。配置同时使用
`permission.edit` 先拒绝所有编辑，再只允许当前 Task 临时目录和两个结果文件，
因此图片、metadata、Skill 及其它结果文件不能通过普通 `write`、`edit` 或
`apply_patch` 修改。该规则限制 OpenCode 常规 read/write/edit/glob/grep 工具；
它不是操作系统沙箱，也不能单独阻止
Bash、Python、Node 或网络访问。需要更强的隔离时必须另外提供容器或 OS 级沙箱。

local 模式只有在应用显式安装 `LocalWorkspaceProvider` 时使用既有
`<runtime>/workspace`、共享 DB、TUI session 历史和结果协议。外部部署应配置
`MEMEMEOW_OPENCODE_WORKSPACE_ROOT`，并由适配层传入 provider；缺失 provider
或独立 `MEMEMEOW_WORKSPACE_CAPABILITY_KEY` 时，non-local 任务 fail-closed。
capability 只携带签名的 Task、attempt、selector、受众和期限；resume 额外绑定
session 与来源 attempt。普通 scope API 只返回当前 Task 的 selector/session 摘要，
不会提供共享 DB 的 session 列表或消息内容。
