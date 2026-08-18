## Why

上传和图片库完整重试目前没有一致、可复用的处理选项确认入口，前端拆分后还丢失了既有反向图片策略传递；同时 `auto_name` 在统一图片处理管线中没有被冻结或传递，导致用户选择可能静默失效。“完整重试所有未就绪”也只处理当前已加载列表并只判断语境状态，与当前 scope 的完整处理语义不一致。

## What Changes

- 新增可复用的图片处理选项对话框，在“上传所选图片”和“完整重试所有未就绪”提交前统一选择本次 `reverse_image_policy=forbid|auto` 与 `auto_name=false|true`；默认禁止联网且不自动重命名。
- 恢复上传和批量完整重试对反向图片策略的序列化、服务可用性提示与稳定错误反馈；`auto` 继续表示允许 Agent 按需调用第三方反向图片服务，不表示必然调用。
- 将 `auto_name` 冻结为图片处理 Job revision 的独立业务选项，不混入 Agent 配置指纹，也不再由 Agent Task 内联执行重命名；活动 Job 只在处理选项兼容时复用。
- 在 Agent 语境与文本 embedding 之间增加条件性的 `auto_rename` 阶段。启用时创建 `image_auto_rename` 叶子 Task；未启用时跳过且不创建 Task。
- 自动重命名 Task 只根据服务端绑定的 Meme、图片 SHA、原 storage key 和当前语境标题生成安全目标名。目标冲突、用户先行重命名或目标变化时不得覆盖文件。
- 名称缺失、名称非法或目标冲突等可降级自动重命名失败必须让叶子 Task 如实失败，但只作为父 Job 的非阻塞警告；Job 仍刷新当前 metadata hash 并继续文本 embedding。目标图片、scope、语境指纹、claim 或存储副作用无法确认时则必须停止 Job。可降级失败可通过受限图片阶段入口独立重试。
- 将“完整重试所有未就绪”改为服务端枚举当前 scope，并按完整处理 Job 的核心阶段有效性选择全部未就绪图片；当前筛选、前端分页和已加载数量不得缩小范围，选择自动重命名也不得波及核心阶段已经就绪的图片。
- 更新任务与 Job 状态展示，使跳过的自动重命名、失败警告、叶子 Task 和恢复动作可观察，同时不把可用的语境或向量伪装为整体失败。

## Capabilities

### New Capabilities

- `image-processing-options`: 定义图片处理 Job 的稳定业务选项、条件性自动重命名阶段和叶子 Task、非阻塞警告、去重与独立重试语义。

### Modified Capabilities

- `image-ingestion`: 上传改为提交统一处理选项，并由图片处理 Job 异步执行可选自动重命名。
- `image-library`: “完整重试所有未就绪”改为覆盖当前 scope 的全部核心未就绪图片，且不受前端筛选或分页影响。
- `frontend-workbench`: 上传与图库完整重试复用同一处理选项对话框，并展示联网可用性、提交状态和自动重命名警告。
- `task-status`: 图片处理 Job 和任务状态增加条件性自动重命名阶段、`image_auto_rename` 叶子 Task、跳过状态与非阻塞警告。

## Impact

- 前端：新增可复用 Vue 对话框及 typed 选项契约，调整上传、图片库、任务详情、API 序列化和服务配置类型。
- FastAPI：统一上传、全库未就绪处理、完整 Job 重试和独立自动重命名阶段的请求/响应契约及可用性校验。
- 图片处理控制面：扩展 Job、阶段和 Task 持久化约束，增加自动重命名 handler、阶段推进、警告聚合、活动去重与 metadata hash 刷新。
- 存储：继续复用按 `meme_id` 的安全重命名与 storage operation 协议；需要只向前数据库迁移和历史 Job 的兼容读取。
- 测试：覆盖对话框可访问性与移动端、multipart/JSON 序列化、全 scope 枚举、四阶段推进、重命名竞态/冲突、警告继续、独立重试、服务重启和历史数据。
- 本 change 依赖 `introduce-image-processing-worker`、`separate-image-pipeline-and-stage-tasks` 和 `add-task-scoped-reverse-image-search` 已定义的 Job、独立阶段和任务级联网策略边界；三个核心阶段保持相对顺序，`auto_rename` 是 Agent 与文本阶段之间的可选扩展，且只有可降级命名失败例外于失败停止规则。依赖 change 必须先同步或归档，最终主规格必须显式合并这两项特化；不得重新把供应商密钥、scope、目标文件名或内部任务关联交给客户端。
