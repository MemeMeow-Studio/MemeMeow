## 1. 数据模型与处理选项契约

- [x] 1.1 确认 `introduce-image-processing-worker`、`separate-image-pipeline-and-stage-tasks` 和 `add-task-scoped-reverse-image-search` 已实施并记录先行同步/归档顺序；依赖 capability 进入主 specs 后，用 OpenSpec update workflow 将本 change 的第四阶段、warning 例外和 standalone 自动重命名合并为对应 `MODIFIED` delta。
- [x] 1.2 以当前 head `0011_harden_operation_grant_association` 新增 Alembic migration，为图片处理 Job 增加默认 `false` 的 `auto_name`，显式删除并重建已有阶段名、阶段状态、Task 阶段和 Task 类型映射 CHECK；覆盖从 0011 升级、干净安装、历史兼容和约束拒绝测试。
- [x] 1.3 同步 SQLAlchemy 模型与启动期兼容 DDL，并更新阶段常量、Task 独占集合、PostgreSQL submit/dedupe/recovery、全局与 scope handler registry、状态反序列化和 retry 拒绝映射，使所有路径一致接受 `auto_rename`、`skipped`、`warning`、`image_auto_rename`；为新增模块、类和函数补齐中文 docstring。
- [x] 1.4 实现后端统一的图片处理选项规范化，覆盖上传 multipart、完整 Job retry 和 scope 级未就绪 JSON 请求，严格拒绝未知非布尔值，并验证安全缺省与 `auto` 服务不可用时的副作用前拒绝。
- [x] 1.5 扩展 Job repository 的创建、复用和 revision 逻辑，独立持久化 `auto_name`，保持其不进入处理配置指纹；活动选项不兼容时返回稳定冲突，终态成功 Job 还必须通过共享核心就绪判定才可复用。
- [x] 1.6 为历史三阶段 Job 增加只读兼容快照，缺失字段时返回 `auto_name=false`、`auto_rename=skipped`，且不插入阶段、不改写历史终态。

## 2. 自动重命名叶子任务

- [x] 2.1 实现 `image_auto_rename` handler 和服务端输入构造，仅绑定当前 scope 的 Meme、图片 SHA、预期 storage key 与语境标题指纹，并从执行时已验证标题派生安全文件名。
- [x] 2.2 扩展按 `meme_id` 的安全重命名与 StorageOperation 协议，在锁定 Meme 和创建操作的同一事务中 compare-and-swap 校验预期 storage key、SHA、Meme revision、Task claim generation 与 attempt；确保排队期间手动重命名获胜、目标冲突不覆盖，未知副作用停止 Job。
- [x] 2.3 注册 `image_auto_rename` Task 类型及其 pipeline/standalone 去重键、Worker lane 和状态序列化，确保 Agent Task payload 不再携带或执行 `auto_name`。
- [x] 2.4 扩展受限 `/images/stages` 提交入口支持 `auto_rename`，从服务端当前状态构造独立 Task；将该 Task 类型加入通用 Task retry 拒绝集合，且不得隐式创建 Job 或下游 Task。
- [x] 2.5 增加自动重命名 handler 测试，覆盖成功、无有效标题、非法派生名、同名冲突、排队期间手动改名、图片或语境变化、claim/fencing 丢失、未知执行、并发去重、存储操作恢复和跨 scope 拒绝。

## 3. 四阶段 Job 编排与警告语义

- [x] 3.1 将新 Job 的阶段顺序改为 `visual -> agent -> auto_rename -> text_embedding`；`auto_name=false` 时持久化 `skipped` 且不创建叶子 Task，`true` 时只在 Agent 有效后创建重命名 Task。
- [x] 3.2 定义并统一应用 `{succeeded, skipped, warning}` 阶段收束集合，更新阶段排序、进度、待执行阶段选择和 Job 完成判断；只有标题/名称/冲突/同图手动改名等可降级失败映射为 `warning`，目标或执行身份失效仍停止 Job。
- [x] 3.3 只在 `auto_rename` 成功、可降级 warning 或 skipped 后从实际 Meme 重新计算 metadata hash，再创建绑定该 hash 的文本 embedding Task；不可降级失败不创建文本 Task，standalone 重命名成功只使旧文本向量过期。
- [x] 3.4 扩展 Job 快照和列表响应，返回 `auto_name`、第四阶段、派生的 `has_warnings` 与有限 warning 摘要，并让进度把 `skipped`、`warning` 视为已收束而不伪造叶子 Task 成功。
- [x] 3.5 增加控制面测试，覆盖四阶段成功、跳过阶段、带 warning 成功、核心阶段失败停止、服务重启恢复、活动选项冲突、终态新 revision 及 metadata hash 在重命名前后的一致性。

## 4. 上传与全 scope 未就绪处理 API

- [x] 4.1 调整上传生产路径，把规范化的 `reverse_image_policy` 和 `auto_name` 一并冻结到每张图片的 Job 并返回，删除 Agent handler 的内联自动命名，同时保持入库成功与 Job 提交失败逐文件隔离。
- [x] 4.2 实现由 Job 终态复用和全 scope 枚举共同调用的核心就绪判定，按当前图片 SHA、视觉配置、Agent 配置、本次反向图片策略、metadata hash 和文本模型验证三类核心产物，明确排除可选重命名 warning。
- [x] 4.3 新增 `POST /images/processing/unready`，只接受处理选项并以数据库游标枚举当前 scope 全部 Meme，逐图短事务创建或复用 Job，返回目标、提交、复用、冲突和失败摘要。
- [x] 4.4 增加 API 测试，覆盖上传与 Job retry 的两项选项序列化、严格布尔解析、安全缺省、`auto` 不可用时无部分副作用、第二页未就绪图片、前端筛选无关、成功 Job 产物失效后恢复、核心就绪图片不因 `auto_name=true` 被处理及逐图部分成功。

## 5. 可复用 Vue 对话框与调用方

- [x] 5.1 在前端类型和 API 层定义 `ImageProcessingOptions`，让上传同时发送两个 multipart 字段，并增加不携带 Meme 列表或分页参数的 scope 级未就绪请求及响应类型。
- [x] 5.2 使用 Vue 3 `<script setup lang="ts">` 实现聚焦单一职责的 `ImageProcessingOptionsDialog.vue`，以 typed props/emits 返回选项；每次打开重置安全默认值，并处理联网能力、busy、取消、焦点限制/恢复、Escape 和窄屏布局。
- [x] 5.3 修改 `UploadWorkspace`，点击上传后先打开共享对话框，确认后用同一组选项上传所有文件，取消时不请求，失败时保留可安全重试的文件与选择并阻止重复提交。
- [x] 5.4 修改 `LibraryWorkspace`，让“完整重试所有未就绪”经共享对话框调用服务端 scope 级端点，不再从当前页或筛选结果拼接 Meme 列表，并按返回分类展示提交摘要。
- [x] 5.5 扩展图片库、任务列表和任务详情的阶段呈现与恢复动作，区分自动重命名 skipped、running、warning 和核心失败，并从图片阶段入口重试 `image_auto_rename` 而非通用 Task retry。
- [x] 5.6 增加 Vue 组件与 API 测试，覆盖两个调用方复用、默认值重置、能力不可用、确认/取消、重复点击、请求字段、warning 文案、键盘焦点闭环和 320px 窄屏不溢出。

## 6. 集成验收与交付检查

- [x] 6.1 使用 `uv` 运行后端单元、API、PostgreSQL migration 和并发/恢复测试，并确认旧三阶段 fixture 与缺省旧客户端请求保持兼容。
- [x] 6.2 运行前端 typecheck、单元测试和构建，修复 typed API、组件契约或状态枚举遗漏。
- [x] 6.3 使用 Playwright 在桌面与移动视口验收上传和完整重试的确认/取消/服务不可用/提交中状态，以及带 warning Job 的展示与独立恢复；检查焦点、文本溢出、遮挡和重复请求。
- [x] 6.4 在依赖 change 已同步或归档后完成 capability delta 对账，确认最终主规格不再保留“仅三个阶段”或“所有自动重命名失败都停止/继续”的冲突表述。
- [x] 6.5 执行 OpenSpec strict validation，并对照全部 delta spec 逐项核验 API 响应、数据库约束、任务历史和 UI 行为；确认未把 scope、目标文件名、供应商凭据或内部任务关联暴露给客户端。
