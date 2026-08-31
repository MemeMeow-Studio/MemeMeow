## MODIFIED Requirements

### Requirement: Agent 只能读取 task-scoped 的候选视图

OpenCode workspace MUST 提供与 task scratch 同级的 `candidate_root` 只读目录和 task-relative manifest。`external_directory` 可以允许 Agent 读取候选视图，但 `edit`、`write` 和 `apply_patch` MUST 拒绝候选目录及其文件。候选目录必须从根到文件拒绝符号链接和路径逃逸。

#### Scenario: Agent 读取候选

- **WHEN** OpenCode 读取当前 Task 的候选 manifest 或候选图片
- **THEN** 读取只返回当前 snapshot 已物化的文件，不允许访问其它 task、scope 或 storage key

#### Scenario: Agent 修改候选

- **WHEN** Agent 尝试编辑、覆盖、删除或通过 apply patch 写入 candidate_root
- **THEN** OpenCode 权限拒绝该操作，task scratch 和结果目录仍按既有规则工作

### Requirement: Agent 环境不得包含视觉 callback 能力

新任务 Runner MUST NOT 注入视觉匹配内部 URL、视觉 callback token 或允许 Agent 自报 top-k/scope 的接口。视觉候选必须来自后端 snapshot 和只读视图；reverse-image callback 凭据若仍需要，必须保持 operation 独立。

#### Scenario: Skill 尝试调用视觉 callback

- **WHEN** Agent 环境缺少视觉 callback 地址或 token
- **THEN** 视觉 Skill 不得因此失败，直接读取已准备的 manifest；视觉接口不再是任务必需能力
