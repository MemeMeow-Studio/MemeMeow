## Purpose

为图片库中的每张表情图片提供可单独读取、可版本化和可恢复的结构化 meme 语境记录，持久保存画面事实、引用、会话含义、研究不确定性及其来源，并作为后续检索索引的稳定数据来源。

## ADDED Requirements

### Requirement: 每张图片必须有对应的版本化 sidecar JSON
系统 MUST 为每张受支持的图片维护同目录的 sidecar JSON，文件名为图片完整文件名追加 `.json`（例如 `cat.png.json`）。外层 JSON MUST 包含 `schema_version`、图片相对路径、文件扩展名、文件大小、内容指纹、语义状态和生成信息；`meme_context` MUST 包含 `summary`、`subjects`、`visible_text`、`references`、`meaning`、`keywords`、`search_queries`、`uncertainties` 以及可选 `source_urls`。未知扩展字段 MUST 被读取方保留并忽略。

#### Scenario: 创建图片元数据
- **WHEN** 一张合法图片成功进入图片库
- **THEN** 系统在图片旁创建合法 JSON，且 JSON 中的相对路径与图片实际路径一致，并将尚未完成语义研究的图片标记为 `pending`

#### Scenario: 读取已知版本元数据
- **WHEN** sidecar JSON 的版本受当前服务支持且内容合法
- **THEN** 系统返回其中的结构化字段，保留未知扩展字段，并区分 `pending`、`partial`、`ready` 和 `repair_required` 状态

#### Scenario: 元数据损坏或版本不兼容
- **WHEN** sidecar JSON 不存在、无法解析、路径不匹配或版本不受支持
- **THEN** 系统将该图片标记为元数据待修复，并允许重新生成，不得将损坏 JSON 当作有效 meme 语境使用

### Requirement: meme 语境字段必须遵守证据边界
系统 MUST 原样保存 `visible_text` 中可见文字，并将未经确认或可能过时的角色、出处、模板、引用和当前语用写入 `uncertainties`。`summary` MUST 可独立阅读且不得把猜测陈述为事实；`references` 只可包含已确认的外部引用；`meaning` 未确认时 MUST 为 `null`。`source_urls` 只可保存少量可回查的关键来源，不承担逐项证据图谱职责。

#### Scenario: 视觉观察只能确认画面事实
- **WHEN** 视觉标注流程只获得图片像素或 OCR 结果，未完成外部研究
- **THEN** 系统只写入可由画面支持的主体、可见文字、摘要和关键词，并将未验证的外部推断记录为不确定项

#### Scenario: 研究结果确认外部引用
- **WHEN** 研究流程以可信来源确认角色、作品、模板或当前会话含义
- **THEN** 系统更新对应的 `references` 或 `meaning`，同时保存来源、生成器和更新时间

#### Scenario: 研究无法收敛
- **WHEN** 候选出处或含义存在冲突、证据不足或可能过时
- **THEN** 系统保留竞争性信息于 `uncertainties`，`references` 和 `meaning` 不写入未经确认的结论

### Requirement: 元数据与图片文件生命周期必须保持一致
系统 MUST 在图片重命名时同步移动并更新 sidecar JSON，在图片删除时删除对应 sidecar；任何单步失败 MUST 保留原图片和原 sidecar，避免产生指向错误图片的有效元数据。

#### Scenario: 图片重命名
- **WHEN** 用户成功将图片改名且目标不存在
- **THEN** sidecar 使用同样的目标文件名移动，且其中的相对路径和指纹字段被更新

#### Scenario: 删除图片
- **WHEN** 图片从图片库移除
- **THEN** 对应 sidecar 不再被图片库视为有效元数据

#### Scenario: 生命周期操作失败
- **WHEN** sidecar 移动、更新或删除失败
- **THEN** 系统返回明确失败结果，原图片与原 sidecar 均保持可用

### Requirement: 字段来源和有效内容必须可追溯
系统 MUST 将视觉标注、研究流程或人工输入的字段与来源、生成时间及模型标识关联保存。重复生成失败时 MUST 保留最近一次有效内容，并记录本次失败状态，不得用空结果覆盖有效内容；人工确认的内容 MUST 不被自动流程静默覆盖。

#### Scenario: 结构化语境写入成功
- **WHEN** 视觉标注、研究流程或人工输入产生符合 schema 的有效内容
- **THEN** sidecar 保存内容及其字段来源、模型标识和生成时间，并更新语义状态

#### Scenario: 语境生成失败
- **WHEN** 生成或研究流程未配置、超时或返回无效内容
- **THEN** sidecar 保留最近一次有效 meme 语境，并记录可诊断的失败状态

### Requirement: sidecar 写入必须是可恢复的
系统 MUST 以完整 JSON 替换的方式提交 sidecar 更新；进程中断或写入失败不得留下一个可被读取方误认为完整的半截 JSON。

#### Scenario: 更新被中断
- **WHEN** sidecar 更新过程中发生写入错误或进程中断
- **THEN** 下次读取时只能得到旧的完整版本或明确的待修复状态
