## Context

当前后端以图片文件系统为主数据源，`SearchService` 只维护一个 `search-cache-v2.json`，并以文件名生成 embedding。视觉描述服务只返回候选文本，不持久化，也不能表达图片的外部引用、梗模板、会话含义和不确定性。本 change 的目标是把 `.agents/skills/research-meme-context/references/output-schema.json` 作为图片语义记录的契约，同时保留本地图片库的可复制、可恢复特性。

## Goals / Non-Goals

**Goals:**

- 为每张图片建立无数据库依赖、版本化且有证据边界的 sidecar meme 语境记录。
- 将画面观察、研究结论、人工修订和不确定项分开保存，防止猜测污染检索。
- 用字段白名单组成稳定的文本输入，替代文件名作为常规 embedding 来源。
- 为已有图片提供幂等的基础元数据迁移/修复路径。

**Non-Goals:**

- 本 change 不实现全文数据库、向量数据库、远程元数据同步或逐项证据图谱。
- sidecar 不存储 embedding 向量；向量仍属于可重建的全局检索索引。
- 不要求自动研究流程在每次上传时访问第三方搜索服务；图片可以先处于 `pending` 或 `partial` 状态。
- 不把视觉模型的猜测自动升级为已确认引用、出处或常见会话含义。

## Decisions

### 1. sidecar 使用技术外层与 `meme_context` 语义内层

每张图片使用 `图片完整文件名.json` 的同目录 sidecar，例如 `data/images/cat.png` 对应 `data/images/cat.png.json`。图片库不预设任何分类目录；用户自行创建子目录时，sidecar 仅在该目录中跟随原图。完整文件名避免 `cat.png` 与 `cat.gif` 发生元数据冲突。

外层保存与文件生命周期相关的字段：`schema_version`、`image.relative_path`、扩展名、大小、sha256、`context_status` 与 `provenance`。内层 `meme_context` 复用研究输出 schema：

```json
{
  "title": null,
  "summary": "独立可读的检索摘要",
  "subjects": [],
  "visible_text": [],
  "references": [],
  "meaning": null,
  "keywords": [],
  "search_queries": [],
  "uncertainties": [],
  "source_urls": []
}
```

`title` 是由 Agent 生成的简短、独立可读标题，未知时为 `null`。它属于语义内容，不是文件名；实际文件名只在用户显式启用自动命名时从已持久化的 `title` 派生，并经过路径安全、长度和冲突处理。后续研究或人工更新 `title` 不自动改变图片路径。

集中式 `metadata.json` 或数据库是备选方案，但会使单图复制、导入和备份不透明；当前项目以本地图片目录为边界，sidecar 的故障范围和迁移成本更低。

### 2. 以状态和字段来源维护证据边界

`context_status` 分为：`pending`（仅有基础文件信息）、`partial`（有可由画面支持的语义字段）、`ready`（研究语境已完成）和 `repair_required`（损坏、不兼容或与文件不匹配）。

视觉模型只可填充 `title`、`summary`、`subjects`、`visible_text` 和 `keywords` 中可由像素或 OCR 支持的内容；仅完成视觉观察的 `title` 不得擅自采用未确认的角色、作品或模板名称。外部角色、作品、模板、台词出处、歌曲、事件与当前会话含义必须经研究流程或人工确认后才写入 `references` 与 `meaning`；研究 Agent 可依据已确认引用生成更准确的 `title`。不能收敛的信息一律进入 `uncertainties`。`provenance` 至少记录生产者类别、模型/流程版本和更新时间，并在人工确认时保护相应字段不被自动覆盖。

这样保留了研究 schema 的简洁字符串数组，不需要建立逐项证据图谱，却能避免把模型猜测误写成检索事实。

### 3. 从字段白名单构造单一 `semantic_document`

检索索引不读取整份 JSON，而是按固定顺序、固定标题、去重和长度上限拼接：

```text
标题：{title}
摘要：{summary}
主体：{subjects}
图片文字：{visible_text}
已确认引用：{references}
常见含义：{meaning}
关键词：{keywords}
```

空字段整段省略。`search_queries` 是后续研究 Agent 或搜索引擎的操作输入，`uncertainties` 是非事实候选，`source_urls` 是回查指针；三者永远不得进入 embedding。`pending` 与 `repair_required` 图片按文件名回退并被报告，`partial` 只使用已填充的画面事实字段，`ready` 使用全部白名单字段。

自动命名先把 Agent 生成的 `title` 写入 sidecar，再从该值派生文件名。派生过程保留原扩展名，清理路径分隔符和不可用字符，并在目标冲突时保留原文件名而不覆盖已有文件。展示标题始终保留自然语言，不被文件名清理结果反向覆盖。

索引记录保存 `semantic_document` 哈希、sidecar schema 版本、图片 sha256 和 embedding 模型。任一白名单字段、图片指纹或模型变化都会使相关索引记录过期；缓存格式至少升级到 v3，使旧的文件名索引不能被误用。

### 4. 统一元数据服务和可恢复的双文件更新

新增元数据服务负责 sidecar 路径计算、schema 校验、哈希计算、状态分类、字段合并与读写。上传、重命名、视觉标注、研究结果导入和检索缓存只通过该边界访问元数据。

sidecar 更新先写同目录临时文件，再原子替换。图片与 sidecar 没有跨文件原子事务，因此重命名采用可回滚顺序：准备新 sidecar，移动图片并提交新 sidecar，最后删除旧 sidecar；失败时恢复原路径和原 sidecar。读取时使用图片 sha256 和相对路径发现断电后的不一致，并标记为 `repair_required`。

### 5. 用幂等任务补齐既有图片，不自动扩张研究范围

元数据初始化/修复任务扫描图片根目录，为缺少或损坏 sidecar 的图片建立最小 `pending` 记录，不默认调用视觉模型、反向图片检索或网页搜索。研究结果可在后续流程中经 schema 校验写回。迁移完成后，使用新索引格式重新生成 embedding 缓存。

## Risks / Trade-offs

- [Risk] 每张图片增加一个小文件，目录遍历和备份开销上升 → 排除 `.json` sidecar，使用紧凑 JSON，并保持单图独立更新。
- [Risk] 图像与 sidecar 在机器断电后不一致 → 用原子 sidecar 替换、sha256 校验和幂等修复任务处理，不让不一致记录进入完整语义索引。
- [Risk] 研究不完整会降低召回 → `partial` 可利用已确认的画面事实；`pending` 回退文件名，但不伪造外部语义。
- [Risk] 语义字段无限增长会抬升 embedding 成本并稀释相关性 → 对数组项数量、单项长度和总 `semantic_document` 长度设定验证上限。
- [Risk] 当前含义会随社群使用变化 → 保存更新时间、流程来源和少量关键 `source_urls`，将不稳定结论标为不确定项并允许重新研究。
