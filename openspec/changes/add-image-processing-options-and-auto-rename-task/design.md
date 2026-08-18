## Context

本 change 的动机见 `proposal.md`，行为边界见本 change 的五份 delta spec。当前生产路径已经有逐图图片处理 Job、持久阶段、pipeline/standalone Task 区分和任务级反向图片策略，但控制面仍固定为 `visual -> agent -> text_embedding`：数据库 CHECK、阶段常量、Task 类型映射和状态快照都只接受三个阶段。

上传接口已经解析 `auto_name` 和 `reverse_image_policy`，但统一 Job 提交只传递后者；旧 Agent handler 仍从 Task payload 读取 `auto_name` 并内联重命名。前端上传 API 又只序列化 `auto_name`。因此同一用户选择在新旧路径中的实际效果不同。

现有 `POST /images/processing` 按请求中的 `page/page_size` 枚举图片，而工作台“完整重试所有未就绪”直接发送当前页、当前筛选结果中语境未就绪的 Meme。它们都不能表达“当前 scope 的全部核心未就绪图片”。此外，文本 metadata hash 包含 `storage_key`，所以重命名必须在文本 embedding 之前完成或明确使已有文本向量过期。

本设计依赖 `introduce-image-processing-worker`、`separate-image-pipeline-and-stage-tasks` 和 `add-task-scoped-reverse-image-search`。其中三个核心阶段保持原相对顺序，`auto_rename` 是 Agent 与文本阶段之间的可选扩展；只有明确列出的可降级命名错误例外于失败停止规则，目标或执行身份失效仍停止 Job。实施和归档时必须保持这些依赖在前，不能把当前 change 单独应用到没有图片处理 Job 的版本。由于 `image-processing` 和 `image-stage-submission` 尚未同步到主 specs，本 change 先由 `image-processing-options` 记录这两项后置特化；依赖同步后、归档本 change 前，必须把最终契约合并为对应 capability 的 `MODIFIED` delta 并再次严格校验。

## Goals / Non-Goals

**Goals:**

- 让上传与 scope 级完整重试共享一份明确、可校验、可冻结的处理选项契约。
- 把自动重命名变成可观察、可去重、可恢复且不会覆盖用户操作的图片阶段叶子 Task。
- 保持核心处理链的真实性：自动重命名失败可降级，但视觉、Agent 和文本 embedding 的有效性不能被 warning 掩盖。
- 让“全部未就绪”由服务端依据当前配置和产物指纹判断，不依赖浏览器加载了多少图片。
- 保持历史三阶段 Job 和旧客户端的安全默认行为。

**Non-Goals:**

- 不让客户端提供自动生成的目标文件名、scope、内部 Task 关联或重命名覆盖策略。
- 不改变手动重命名 API 的用户主导语义，也不增加批量重命名功能。
- 不保证 `reverse_image_policy=auto` 一定联网，不改变第三方检索的计量、授权或供应商配置。
- 不让 standalone 自动重命名 Task 隐式推进完整 Job 或创建文本 embedding Task。
- 不引入新的全局前端状态管理、通用表单框架或新的视觉体系。

## Decisions

### 1. 使用一份规范化处理选项值对象

前后端共同使用以下业务形状：

```text
ImageProcessingOptions {
  reverse_image_policy: "forbid" | "auto"
  auto_name: boolean
}
```

后端提供唯一规范化入口，上传表单、完整重试 JSON 和完整 Job retry 都调用它。缺失字段分别归一为 `forbid` 和 `false`；未知枚举或非布尔 `auto_name` 在创建任何 Job 前拒绝。`auto` 的服务可用性也在批量副作用开始前统一校验，防止同一请求只创建一部分 Job 后才发现能力不可用。

前端新增 `ImageProcessingOptionsDialog.vue`。它只接收 `open`、反向图片服务能力和 `busy`，并发出 typed `confirm(options)` 或 `cancel` 事件；它不持有文件、不调用 API，也不知道触发者是上传还是完整重试。`UploadWorkspace` 和 `LibraryWorkspace` 分别保留待执行动作、请求和结果状态。每次打开都重置为安全默认值，不把上次的联网授权隐式延续到下一次操作。

选择这一边界是为了让组件真正复用，同时维持 Vue 的 props-down/events-up 单向数据流。没有引入返回 Promise 的全局 dialog service，因为当前只有两个明确调用方，额外的命令队列和注入层不能减少复杂度。

### 2. `auto_name` 独立持久化，不进入处理配置指纹

`image_processing_jobs` 增加非空布尔列 `auto_name`，默认 `false`。它与已有 `reverse_image_policy` 一样属于 Job revision 的冻结业务选择，但不进入 `processing_config_hash`。原因是自动重命名改变阶段编排和 storage key，不改变视觉模型、Agent skill/model 或 embedding 模型的配置身份；把它混入配置指纹会错误地使有效 Agent 语境失效。

Job 的活动复用在同一事务和目标锁内比较图片 SHA、处理配置指纹、`reverse_image_policy` 与 `auto_name`：

- 四者兼容时复用同一活动 Job；
- 反向图片策略不同继续返回既有 `generation_policy_conflict`；
- 仅 `auto_name` 不同返回 `processing_options_conflict`；
- 显式重试终态 Job 可以用新选项创建新 revision，旧 revision 不变；
- 终态成功 Job 只有在两项选项一致且核心产物仍有效时才可复用。

Agent Task payload 不再包含或继承 `auto_name`。其去重键继续只由目标版本、Agent 配置和冻结的反向图片策略决定。这样“是否改文件名”不会产生两份竞争写入相同语境的 Agent Task。

### 3. 把自动重命名建模为条件性第四阶段

新 Job 的规范阶段顺序为：

```text
visual -> agent -> auto_rename -> text_embedding
```

所有新 Job 都有可查询的 `auto_rename` 阶段事实：`auto_name=false` 时创建为 `skipped` 且没有 `task_id`；`auto_name=true` 时从 `queued` 开始，并在 Agent 产物有效后创建或复用 `image_auto_rename` Task。`skipped` 视为已收束，因此进度计算不会停在第三阶段；但它不伪造一次 Task 执行。

普通叶子 Task 状态机保持 `queued -> running -> succeeded|failed`。只有父阶段增加 `skipped` 和 `warning`：

| 自动重命名结果 | 叶子 Task | `auto_rename` 阶段 | Job 行为 |
|---|---|---|---|
| 未启用 | 不存在 | `skipped` | 继续文本 embedding |
| 成功 | `succeeded` | `succeeded` | 刷新 hash 后继续 |
| 可降级命名失败 | `failed` | `warning` | 保留当前名称，刷新 hash 后继续 |
| 目标或执行身份失效 | `failed` | `failed`/`unknown_execution` | 停止 Job，不创建文本 Task |

`warning` 是已收束、可继续的阶段终态，不是 Task 状态。可降级集合只包括标题缺失、候选名非法、目标名称冲突和同一图片已被用户手动改名；图片删除或 SHA/scope/语境指纹变化、claim 丢失、fencing 失败、存储副作用未知都不是 warning。Job 快照从阶段状态派生 `has_warnings` 和有限 warning 列表，不另存一份容易漂移的布尔值；阶段 `error` 是历史事实。视觉、Agent、文本 embedding 或不可降级自动重命名失败仍按原规则停止 Job。自动重命名 warning 后若核心阶段完成，Job 可以为 `succeeded`，且顶层 `error` 保持空值。

阶段排序、进度、待执行阶段选择和 Job 收束统一使用 `SETTLED_STAGE_STATUSES={succeeded, skipped, warning}`；它只决定是否可以前进，不改变失败 Task 的事实。`failed`、`blocked` 和 `unknown_execution` 不属于 settled 集合。

选择阶段而不是 Agent handler 内联步骤，是因为重命名有独立副作用、并发条件、错误与重试语义。把它留在 Agent Task 中无法准确区分“语境成功、文件名失败”，也无法在不重跑 Agent 的情况下恢复。

### 4. 重命名 Task 只绑定输入事实，不持久化客户端目标

pipeline 或 standalone `image_auto_rename` Task 的服务端 payload 包含：

- `meme_id` 与目标图片 SHA-256；
- `expected_storage_key`；
- 当前规范化语境标题的指纹；
- pipeline/standalone 模式、Job revision（如有）和阶段身份。

payload 不包含客户端字段，也不保存预先信任的目标路径。handler 获得 Task claim 后重新加载当前 scope 的 Meme 和语境，验证 SHA、storage key 和标题指纹，再由当前标题生成安全 basename、保留原扩展名。标题缺失或清理后为空时以稳定错误失败。

实际文件与数据库更新复用并扩展现有按 `meme_id` 的安全重命名和 `StorageOperation` 恢复协议。存储协调器新增自动任务专用的 compare-and-swap 输入：`expected_storage_key`、图片 SHA、Meme revision、Task claim generation 和执行 attempt；它必须在锁定 Meme、创建 storage operation 的同一事务中原子校验，不能只依赖 handler 更早的一次读取。校验后由该 storage operation 独占本次副作用；目标已存在时不覆盖，源 storage key 已变化时视为用户或其他任务已经获胜。进程在文件移动与数据库提交之间退出时，由存储操作日志恢复到单一可证明结果；无法确认结果时按 `unknown_execution` 停止 Job，而不是降级后继续。

Task 去重键包含 scope 内部身份、模式、Meme、图片 SHA、预期 storage key 和标题指纹。它不包含目标文件名；目标始终由执行时验证后的服务端状态派生。这同时允许相同输入的并发提交复用，又让手动重命名或语境变化自然形成不兼容输入。

### 5. metadata hash 只在重命名阶段收束后冻结给文本阶段

当前 metadata hash 包含 `storage_key`。因此 Agent 成功后不再立即把 hash 当作文本阶段最终输入：

1. Agent 成功，Job 进入 `auto_rename`。
2. 自动重命名成功、warning 或 skipped 后，Worker 重新加载 Meme。
3. Worker 从实际 `storage_key` 和当前语境计算 metadata hash，持久化到 Job。
4. 文本 Task payload 绑定该 hash，执行前和写回前继续做现有有效性校验。

若自动重命名成功，旧 storage key 对应的文本 embedding 由现有有效性逻辑判为过期；若发生可降级 warning，当前 storage key 的 hash 成为本次文本输入。不可降级失败不冻结新的文本输入，也不创建文本 Task。standalone 自动重命名成功不会自动创建下游 Task，但会让旧文本 embedding 因 hash 不匹配而失去当前资格，随后可由完整重试或独立文本阶段显式恢复。

### 6. 新增明确的 scope 级未就绪动作端点

保留现有分页 `POST /images/processing` 的兼容行为，新增 `POST /images/processing/unready` 作为“完整重试所有未就绪”的唯一端点。请求体只接收两项处理选项，不接收 Meme 列表、筛选、页码或 scope。这样不会把旧分页调用突然扩大为全库副作用，也能从 API 形状上阻止客户端集合成为授权事实。

服务端先校验选项和 `auto` 能力，再按当前请求 scope 用数据库游标分批读取所有 Meme。每张图片的核心就绪按以下当前事实判断：

- 图片 SHA 对应的当前视觉模型与预处理版本产物有效；
- Agent 语境对当前图片、Agent 配置和本次 `reverse_image_policy` 有效；
- 文本 embedding 匹配当前 metadata hash、模型版本和维度。

`auto_name` 不参与核心就绪判断，所以选中自动重命名不会把原本核心就绪的图片加入目标集合。目标图片逐一在短事务中创建/复用 Job，不建立全 scope 大事务或阶段屏障；单图冲突与提交错误记录在结果中，其余图片继续。

响应包含 `target_count`、`submitted_count`、`reused_count`、`conflict_count`、`failed_count` 和逐图的有限提交结果。它只描述 Job 提交，不声称异步阶段已经完成。第一版保持同步枚举和提交，不额外引入批次 Task；如果实际规模证明请求时长不可接受，再以不改变目标选择语义的批次控制面演进。

### 7. API 与前端状态使用同一术语

上传继续使用 multipart，但前端 API 改为接收完整 `ImageProcessingOptions` 并同时附加 `reverse_image_policy`、`auto_name`。逐文件成功结果以 `processing_job_id` 为权威标识；旧视觉任务字段只作为既有兼容别名，不能再用于推断叶子 Task。

`POST /images/stages` 扩展 `stage=auto_rename`。该分支只接受 `meme_id` 和阶段名，按当前服务端状态绑定输入；客户端提交目标名时严格拒绝。通用 `/tasks/{task_id}/retry` 将 `image_auto_rename` 纳入图片阶段拒绝集合。

Job 状态增加 `auto_name`、第四阶段、`has_warnings` 和 `warnings`。工作台把后端枚举映射为面向用户的中文状态：`skipped` 表示“未启用”，`warning` 表示“处理完成，自动重命名未完成”。warning 使用现有警告色、文字与恢复动作共同表达，不只依赖颜色。

对话框沿用现有工作台的表单密度、6px 控件圆角、44px 以上触达区和状态色。桌面保持紧凑的单列字段顺序，窄屏操作按钮可改为单列；打开时聚焦标题后的首个选项，焦点限制在 modal，Escape 等价于取消，关闭后恢复到触发按钮。反向图片服务不可用时禁用 `auto` 并在字段附近解释原因，不能用全局 toast 代替字段状态。

### 8. 数据库迁移保持只向前兼容

以当前 Alembic head `0011_harden_operation_grant_association` 为 `down_revision` 新增 revision，并执行以下变化：

- `image_processing_jobs.auto_name BOOLEAN NOT NULL DEFAULT FALSE`；
- `image_processing_stages.stage` CHECK 加入 `auto_rename`；
- 阶段状态 CHECK 加入 `skipped` 和 `warning`；
- Task 的 `image_stage` CHECK 加入 `auto_rename`；
- Task 类型/阶段约束加入 `image_auto_rename <-> auto_rename` 映射。

已有数据库中的同名 CHECK 仍只允许三阶段，因此 migration 必须显式删除并重建旧约束，不能用“同名存在即跳过”的 `IF NOT EXISTS`。启动期兼容 DDL 也必须检查约束定义或使用新版本约束名。ORM 约束、启动期兼容 DDL 和正式 migration 必须保持同一集合，并分别测试从 0011 升级和干净安装。迁移不为历史 Job 批量插入阶段行：读取历史三阶段 Job 时，快照层只读合成 `auto_name=false` 与 `auto_rename=skipped`；新 Job 才持久化第四阶段。这避免大表回填和把兼容默认误写成历史用户选择。

## Risks / Trade-offs

- **[全 scope 同步枚举在超大图片库中增加请求时长]** -> 使用数据库游标、短事务和逐图隔离；记录目标数与耗时，达到实际阈值后再引入持久批次控制面。
- **[自动重命名成功使既有文本向量立刻过期]** -> pipeline 必须在其后生成新文本 embedding；standalone 响应和 UI 明确提示核心处理需要恢复，绝不继续使用旧 hash 的向量。
- **[父 Job 成功而叶子 Task 失败可能让调用方误判]** -> 保持叶子 `failed`，Job 同时返回 `has_warnings` 与结构化 warning；前端分别表达核心完成和可选动作失败。
- **[活动 Job 的新增选项比较提高冲突概率]** -> 在创建边界返回稳定、可定位的冲突，不通过并行 Job 解决共享产物竞争；终态后允许显式新 revision。
- **[应用回滚后旧 Worker 不理解第四阶段]** -> 回滚前停止新图片处理提交并排空或收束活动四阶段 Job；保留加法式数据库迁移和历史行，不在紧急回滚中删除列或 CHECK 值。
- **[上游未归档 change 的三阶段文字与本 change 暂时并存]** -> 按声明依赖顺序实施和归档，在同步主规格时以本 change 的条件性第四阶段和 warning 例外更新最终契约，并在归档前再次执行跨 change 校验。

## Migration Plan

1. 确认三个依赖 change 已实施；它们必须先于本 change 同步或归档。依赖 capability 进入主 specs 后，用 OpenSpec update workflow 把本 change 的三阶段/失败停止和独立阶段特化合并为对应 `MODIFIED` delta。
2. 先部署只向前数据库 migration，扩展列并显式重建 CHECK；旧应用仍可写三阶段 Job，新增列使用安全默认值。
3. 部署后端的规范化选项、四阶段 Worker、`image_auto_rename` handler、状态快照和 scope 级未就绪端点；在启用入口前完成历史读取兼容测试。
4. 部署前端 typed API、复用对话框和 warning 展示，使上传与完整重试开始发送两项选项。
5. 观察活动 Job、warning 数量、名称冲突和全 scope 请求耗时；确认没有旧 Agent payload 继续执行内联重命名。
6. 回滚时先停止新提交并等待活动四阶段 Job 收束，再回滚应用；数据库保留扩展结构。只有确认不存在新阶段历史依赖后，才在后续独立维护窗口考虑破坏性 schema downgrade。
