## Context

OpenCode 运行器目前在构造时固定 `<runtime>/workspace` 和 `<runtime>/opencode.db`，host CLI、TUI、配置文件、子进程 `cwd` 与 Compose executor 都直接引用该 workspace。Task 结果已经按业务 Task 分目录并有限保留，每张图片也已经使用独立 session；本 change 不改变这两个事实。

应用 scope 的可信来源已经由持久 `Task.scope_id`、Worker 恢复和 service claim 契约定义，但 OpenCode 层没有接收可信 workspace 上下文的扩展点。executor 又刻意拒绝任意 `workdir`、环境和命令，因此新增能力必须传递不可解释为路径的受控 selector，不能把原始 scope 或目录开放为普通请求字段。

OpenCode 1.18.18 支持顶层 `permission.external_directory`。read、write、edit、glob 和 grep 等常规文件工具会检查当前 instance directory/worktree；Shell 只对部分可识别命令提取路径，`python -c` 等可以绕过，因此该权限只能承担常规工具边界。

开源仓库当前已有 local 单 workspace 数据和 session 历史。迁移必须原地兼容，不能为了通用扩展点创建每 scope DB 或移动既有 runtime。

## Goals / Non-Goals

**Goals:**

- 用一个小型 provider 接口解耦可信业务 scope 与 OpenCode 目录选择。
- 共享一个 OpenCode DB，同时让 CLI、配置、cwd、图片/metadata 视图和 resume 使用一致 workspace 上下文。
- 让 Compose executor 接受带签名任务绑定的 opaque selector 而不是任意路径。
- 保持 local 默认路径、TUI、session 历史、并发和结果协议兼容。

**Non-Goals:**

- 不在公共核心中引入账户、计费、订阅或特定部署的 scope 解析。
- 不建立每 scope DB、每 Task 容器或持久上传/任务 workspace 表。
- 不禁止 Bash、Python、Node 和网络研究，不承诺 OS 级用户文件隔离。
- 不改变图片检索候选资格、callback 授权、Agent 输出 schema 或业务写回事务。

## Decisions

### 1. provider 只接收可信任务上下文并返回逻辑 workspace 描述

OpenCode 运行器新增窄接口，概念结果为：

```text
ResolvedWorkspace
  selector: opaque stable identifier
  directory: OpenCode --dir/cwd
  config_file: opencode.json
  config_dir: .opencode
  images_root: current workspace image view
  metadata_root: current workspace metadata view
  skill_root: read-only Skill view
  task_scratch_root: per-Task temporary workspace
  task_results_root: existing controlled result handoff root
```

业务层在完成 Task/claim 恢复后调用 provider。显式 local 应用装配安装 `LocalWorkspaceProvider`，固定返回现有 `<runtime>/workspace`；适配层可以从可信 `Task.scope_id` 生成稳定 opaque selector，并把它解析到配置的 workspace 根下。核心验证 selector 字符集、最终路径 containment、各级 `lstat` 和非符号链接约束。普通 API 模型不增加 scope、path 或 workdir 字段。非 local 上下文没有 provider、provider 映射失败或返回 local selector 时全部 fail-closed。

不让 provider 直接返回未验证任意路径，因为这会把 executor 当前的路径边界变成新的命令执行接口。也不在 OpenCodeRunner 内查询账户或 scope 数据库，因为这会把通用执行器与应用身份模型耦合。

### 2. DB 固定在 runtime 根，workspace 只控制项目与配置上下文

所有执行继续使用 `<runtime>/opencode.db`。local 模式继续使用 `<runtime>/workspace`；外部 provider 可以在受控根下采用以下布局：

```text
<runtime>/opencode.db
<workspace-root>/<opaque-selector>/
  workspace/
    tasks/<task-id>/
      opencode.json
      .opencode/
  images/
  metadata/
  skills/
```

`--dir` 和 `cwd` 指向 `workspace/`，`OPENCODE_CONFIG` 与 `OPENCODE_CONFIG_DIR` 指向当前 Task 的 `tasks/<task-id>/` 配置；`images/`、`metadata/` 和 Skill 视图只读，只有受控配置与当前 `tasks/<task-id>` 临时目录可写。按 Task 生成配置是为了让同一 workspace 的并发任务各自拥有精确结果 allow 规则，避免最后一次配置写入覆盖另一任务。`task_results_root` 明确指向现有 `<runtime>/task-results/<business-task-id>`，只允许当前 Task 的两个受控结果文件。若容器挂载后某个视图位于 `--dir` 外，provider 必须返回其实际容器路径；配置生成器按最后匹配优先语义先写 catch-all deny，再写精确 allow glob。适配层负责实际创建、挂载和清理 scope 视图，并必须保证获准的图片、metadata 和 Skill 视图从根到内容不含符号链接或特殊节点；核心只消费解析后的描述，不对输入树做递归扫描。若适配层遗漏了允许根内部的符号链接，OpenCode `external_directory` 可能跟随它，不能将该机制描述为阻止读穿或提供 OS 级隔离。

现有 `<runtime>/task-results/<business-task-id>/result.json.tmp` 结果路径、原子 rename、大小限制和后端二次校验保持不变；workspace 内 `tasks/<task-id>` 只承载临时研究数据。这样本 change 不修改 `agent-executor` 的结果 capability，也不引入同一业务 Task 多 attempt 争用新的结果路径。attempt/claim 防晚到写回继续由已有 resume 契约负责。

每次进程启动都显式设置同一个 `OPENCODE_DB`，不根据 selector 派生 DB。受信 local TUI 和运维诊断仍有一个权威 session 来源；普通 scope API 只允许返回当前 Task 已持久绑定的 session 摘要，不能把共享 DB 变成 session 枚举接口。workspace 只改变当前工具目录和配置上下文。

替代方案是每 scope 一个 DB。它能减少共享 DB 中的逻辑混淆，但会改变 TUI/历史语义、增加迁移和备份实体，也不解决宿主文件视图，因此不采用。

### 3. session/workspace 绑定作为任务 attempt 元数据持久化

首次执行产生 session 后，Task/attempt 的现有受控恢复元数据与 opaque selector 原子记录。host runner 在传递 `--session` 前比较当前 provider 结果与已记录 selector。进入 executor 时，API/provider 从可信持久绑定签发短期 workspace capability，claims 至少包含 `task_id`、`attempt_id`、`workspace_selector`、`audience` 和 `exp`，并在 resume 时额外包含 `session_id` 与 `resume_of_attempt_id`；executor 验证 capability 后才启动进程。任一缺失或不一致都返回稳定 `opencode_workspace_mismatch`，不启动 OpenCode。

绑定不直接查询或修改 OpenCode SQLite 内部表。OpenCode DB schema 不是应用契约，直接读取会增加版本耦合；业务 Task/attempt 本来就是允许恢复 session 的权威来源，在同一处记录 selector 更容易做原子校验。客户端仍不能直接提交 session，现有持久 Task 恢复链不变。

### 4. executor 协议增加 opaque selector 与签名 workspace capability

固定 executor 请求允许列表增加 `workspace_selector` 和 `workspace_capability`，但不增加 `scope_id`、`workspace_path`、`workdir` 或通用环境字段。selector 仅允许保守标识字符和长度，executor 在配置的 workspace 根下拼接固定布局并再次执行 containment、类型和 symlink 检查；capability 使用独立验证材料或从 executor service secret 域分离派生的密钥验证，未知、过期、错误受众、Task/attempt/session 不匹配或未装配 selector fail-closed。capability 的签发输入必须来自可信 Task/attempt 记录，不能从客户端 payload 透传。

首次 attempt 将 selector 写入 executor 的 attempt 元数据用于诊断；权威验证来自签名 capability 和 API 持久绑定，因此 executor 重启后不会因为进程内状态丢失而接受未绑定请求。resume capability 同时绑定源 attempt 和 session，selector 必须与 API 持久记录相同。Task 最终结果继续写入现有 `/runtime/task-results/<business-task-id>` 协议，并通过 `task_results_root` 的精确 allow 规则提供给 OpenCode；全局队列、并发 slot 和取消仍由共享 executor 管理，不按 workspace 复制。

`image_relative_path` 仍是固定结构化相对路径，但解析根改为当前 selector 对应的 `images_root`，而不是全局图片根；API 和 executor 都必须在各自容器视图中验证同一逻辑根，普通客户端不能通过相对路径选择其它 selector。Skill 继续由稳定只读挂载提供，并由每个 workspace 的配置允许其容器路径；外部 workspace 必须通过真实 Skill 发现测试。

不传目录是为了保留 executor 的固定结构化任务边界；不让 executor 自行查询应用数据库，是为了维持 API/Agent control 网络与 data backend 的分离。签名 capability 让 executor 无需数据网络也能验证 Task/attempt/workspace 绑定。

### 5. OpenCode 配置按 workspace 幂等生成

公共 provider/model 配置模板保持现状，并加入对象形式的 `permission.external_directory`。每个 Task 在当前 workspace 的临时目录原子写入自己的 `opencode.json`，权限保持仅运行用户可写；项目配置继续禁用，避免 workspace 内容覆盖受控配置。OpenCode 使用最后匹配规则，因此对象规则先写 `"*": "deny"`，再对 provider 已验证的 Skill、图片、metadata、当前 Task 临时根和结果目录父级写 allow 规则；两个结果文件的精确可写范围另由 `permission.edit` 先拒绝全部编辑、再允许当前 Task 临时目录和两个结果文件表达，并在最后拒绝服务端生成的配置。不能使用会把合法外部视图一并拒绝的标量 deny，也不能把 catch-all 放在允许项之后，不能从 prompt、图片 JSON 或客户端字段生成允许项。

`external_directory` 约束 OpenCode 常规文件工具，但 Bash/Python/Node 仍按现有 `agent-runtime-isolation` 能力保留。需要严格 scope 文件隔离的宿主必须额外使用独立 Unix 用户、mount namespace 或每任务容器；本 change 不伪装已提供该能力。

### 6. local 模式原地兼容

显式安装的 local provider 使用 selector 语义 `local`，但物理目录仍是旧 `<runtime>/workspace`，不会变成 `<workspace-root>/local/workspace`。现有 DB、workspace 配置、TUI 启动和 task-results 保持原位；只有明确的 local 应用装配才能选择该 provider，非 local 环境缺失 provider 时不回退。升级不执行数据搬迁。

测试同时锁定默认路径和 provider 路径，避免后续重构无意把 local 历史分裂到新目录。

## Risks / Trade-offs

- [共享 DB 中仍可看到所有 session 元数据] -> workspace 绑定阻止任务恢复串用，普通 scope API 不提供共享 session 列表；只有受信 local TUI/运维诊断可读取全局历史，本 change不宣称 DB 元数据物理分区。
- [OpenCode 权限对象或目录判断在版本升级后变化] -> 固定受支持版本做真实 read/glob/write 越界测试，升级 OpenCode 时把权限回归作为门禁。
- [opaque selector 到物理目录的映射漂移会使 resume 失败] -> selector 必须由宿主稳定映射并随 Task 持久化；不允许失败时回退 local。
- [多个 scope 增加配置和持久目录数量] -> 配置按需幂等生成，scope 生命周期由适配层管理，Task 目录沿用有限保留策略。
- [Bash 可以绕过常规文件工具权限] -> 文档和测试明确残余边界；宿主控制面和凭据继续依赖容器/网络隔离，严格文件隔离留给额外 OS 沙箱。
- [允许输入根内部的符号链接可能被常规工具跟随] -> provider 必须维护视图无符号链接不变量；核心不做每任务递归扫描，严格防护留给 provider 生命周期校验和额外 OS 沙箱。
- [workspace capability 增加签发和轮换状态] -> 复用 executor service identity 的域分离签名材料或独立 verifier，限制 audience/TTL 并覆盖旧 key 验证窗口；不把 capability 或 selector暴露给普通客户端。
- [与正在演进的 executor 和 session resume change 发生契约重叠] -> 实现前核对 active change 最终形状，只扩展允许字段和 attempt 元数据，不复制其状态机、取消或结果协议；现有 task-results 路径保持不变。

## Migration Plan

1. 先加入 provider 数据结构和显式 local provider，保持 local 应用装配的所有调用点行为不变；非 local 工厂缺 provider 的测试先固定为 fail-closed。
2. 将 host CLI、TUI、配置生成、结果目录和环境构造改为使用一次解析出的 workspace 描述，并补齐 local 回归。
3. 持久化 session/workspace 绑定，在首次执行、失败重试和 resume 路径 fail-closed 校验。
4. 扩展 executor selector/capability 请求、验证材料、TaskState/attempt 元数据和固定目录解析，再增加两个 selector 共享 DB 的集成测试；保留现有 task-results 协议。
5. 最后开放适配层安装外部 provider，并验证图片、metadata、Task 目录与 `external_directory` 的真实 OpenCode 行为。

回滚时只有显式 local 应用可以恢复 local provider；不修改或删除共享 DB。已经带非 local selector 的未完成 Task 不允许自动回退 local，必须保持失败/待恢复，直到对应 provider 与 capability verifier 恢复。
