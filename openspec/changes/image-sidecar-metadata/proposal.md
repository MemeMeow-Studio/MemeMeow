## Why

当前图片库只有图片文件和一个由文件名生成的全局检索缓存；VLM 生成的描述只在接口响应中短暂存在，也无法表达表情包的主体、原文、外部引用、会话含义和不确定性。这样既不能稳定复用研究成果，也会让 embedding 以低信息量文件名为输入。现在需要以按图片关联的 JSON 持久化一份有证据边界的 meme 语境记录，并以其生成检索索引。

## What Changes

- 为每张受支持的图片维护一个同目录的 sidecar JSON，使用 `图片完整文件名.json` 命名（例如 `cat.png.json`），避免同名不同扩展名冲突。
- 定义版本化的元数据结构：外层保存图片相对路径、文件指纹和生成信息；`meme_context` 使用研究输出 schema，保存 Agent 生成的可读 `title`，以及 `summary`、`subjects`、`visible_text`、`references`、`meaning`、`keywords`、`search_queries`、`uncertainties` 与可选 `source_urls`。
- 上传图片时创建带 `pending` 语义状态的基础元数据；视觉标注或研究结果只更新其有权确认的字段，并保留来源、模型和时间。
- 显式启用自动命名时从已持久化的 `title` 派生安全文件名；普通标题更新不隐式改动图片路径。重命名图片时同步移动并更新 JSON；删除或清理图片时不留下孤立元数据文件。
- 让批量视觉标注和研究结果持久化每张图片的独立成功/失败状态，单张失败不影响其他图片或已确认内容。
- 检索缓存继续作为可重建的派生索引；生成 embedding 时只使用 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、`meaning` 和 `keywords` 的固定组合，绝不使用 `search_queries`、`uncertainties` 或 `source_urls`。
- 将未确认的角色、出处、模板和当前语用保存为不确定项，不得写入作为事实的摘要、引用或 embedding 输入。
- 对缺失、损坏、版本不兼容或路径不匹配的 JSON 提供可识别状态，并允许重新生成，而不是阻塞整个图片库。

## Capabilities

### New Capabilities

- `image-metadata`: 为图片提供版本化、可持久化且有证据边界的 sidecar JSON meme 语境记录及其生命周期管理。

### Modified Capabilities

- `image-ingestion`: 上传和重命名流程需要创建、同步维护图片元数据。
- `image-labeling`: 视觉标注、研究结果与批量任务需要按字段来源持久化到对应图片元数据。
- `meme-search`: 检索索引应从经过白名单筛选的 meme 语境字段构造语义输入，并在元数据变化后支持可控重建。

## Impact

- 主要影响 `backend/` 的图片路径、上传/重命名、标注和检索服务，以及对应的 FastAPI 响应和任务测试。
- 需要新增元数据读写、schema 校验、字段来源与新鲜度、原子更新和迁移/修复逻辑；不引入数据库，优先使用本地 JSON 文件。
- 前端图片库和标注界面可逐步展示或编辑持久化字段，但本变更的后端契约应先稳定。
- 现有 `search-cache-v2.json` 仍可被替换和重建；旧版 Streamlit pickle 缓存不纳入兼容范围。
