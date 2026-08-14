# image-metadata Specification

## Purpose
为图片库中的每张 Meme 在 PostgreSQL 中保存版本化、可追溯且受 scope 隔离的结构化语境记录，使图片指纹、证据边界和语义生命周期成为可事务更新的稳定业务事实。
## Requirements
### Requirement: 每张 Meme 必须有版本化的数据库元数据
系统 MUST 为每张受支持图片维护一条与 `meme_id` 和 `scope_id` 关联的 PostgreSQL 元数据记录。记录 MUST 包含 schema 版本、相对存储路径、扩展名、文件大小、内容指纹、语义状态、`meme_context` 和生成信息；`meme_context` MUST 支持可为 `null` 的 `title`，以及 `summary`、`subjects`、`visible_text`、`references`、`meaning`、`keywords`、`search_queries`、`uncertainties` 和可选 `source_urls`。未知扩展字段 MUST 在读取和再次写入后保留。

#### Scenario: 创建 Meme 元数据
- **WHEN** 一张合法图片成功进入图片库
- **THEN** 系统在同一 scope 中创建稳定 Meme 记录和合法元数据，将尚未完成语义研究的记录标记为 `pending`

#### Scenario: 读取已知版本元数据
- **WHEN** 数据库元数据版本受当前服务支持且内容合法
- **THEN** 系统返回结构化字段，保留未知扩展字段，并区分 `pending`、`partial`、`ready` 和 `repair_required` 状态

#### Scenario: 元数据版本不兼容
- **WHEN** 数据库中的元数据版本不受当前服务支持或必需字段无效
- **THEN** 系统将该 Meme 标记或报告为待修复，不得将无效语境用于检索

### Requirement: meme 语境字段必须遵守证据边界
系统 MUST 原样保存 `visible_text` 中可见文字，并将未经确认或可能过时的角色、出处、模板、引用和当前语用写入 `uncertainties`。`title` MUST 是简短、独立可读的自然语言标题，未知时为 `null`，且不得把未确认引用写成事实；`summary` MUST 可独立阅读且不得把猜测陈述为事实；`references` 只可包含已确认的外部引用；`meaning` 未确认时 MUST 为 `null`。

#### Scenario: 视觉观察只能确认画面事实
- **WHEN** 视觉标注流程只获得图片像素或 OCR 结果，未完成外部研究
- **THEN** 系统只写入可由画面支持的标题、主体、可见文字、摘要和关键词，并将未验证的外部推断记录为不确定项

#### Scenario: 研究结果确认外部引用
- **WHEN** 研究流程以可信来源确认角色、作品、模板或当前会话含义
- **THEN** 系统更新对应的 `references` 或 `meaning`，同时保存来源、生成器和更新时间

#### Scenario: 研究无法收敛
- **WHEN** 候选出处或含义存在冲突、证据不足或可能过时
- **THEN** 系统保留竞争性信息于 `uncertainties`，`references` 和 `meaning` 不写入未经确认的结论

### Requirement: 数据库元数据必须校验图片身份
系统 MUST 使用相对存储路径、扩展名、文件大小和 SHA-256 内容指纹验证数据库记录仍指向同一图片。图片缺失或指纹不匹配时，系统 MUST 将记录视为 `repair_required` 或目标已变化，不得把旧语境提交给新文件。

#### Scenario: 图片内容被外部替换
- **WHEN** 同一路径的图片内容与数据库中的 SHA-256 或大小不一致
- **THEN** 系统拒绝把原元数据视为有效，并将该 Meme 报告为待修复

#### Scenario: 长任务执行期间图片变化
- **WHEN** 语境任务提交后、写回前图片内容指纹发生变化
- **THEN** 系统以 `target_changed` 失败，不覆盖当前 Meme 的有效语境

### Requirement: 元数据与图片生命周期必须保持一致
系统 MUST 在图片重命名时更新同一 Meme 的存储路径，在图片删除时使对应 Meme 和元数据不再可访问。任何单步失败 MUST 返回明确失败结果，并通过补偿或恢复流程保留操作前的有效状态。

#### Scenario: 图片重命名
- **WHEN** 用户成功将图片改名且目标不存在
- **THEN** 同一 `meme_id` 的存储路径被更新，图片指纹和语境内容保持有效

#### Scenario: 删除图片
- **WHEN** 图片成功从图片库删除
- **THEN** 对应 Meme 元数据不再被列表、检索或语境接口返回

#### Scenario: 生命周期操作失败
- **WHEN** 文件存储或数据库更新失败
- **THEN** 系统不报告成功，并恢复到原资源可用状态或记录可恢复的未完成操作

### Requirement: 字段来源和有效内容必须可追溯
系统 MUST 将视觉标注、研究流程或人工输入的字段与来源、生成时间及模型标识关联保存。重复生成失败时 MUST 保留最近一次有效内容并记录本次失败，不得用空结果覆盖有效内容；人工确认的内容 MUST 不被自动流程静默覆盖。

#### Scenario: 结构化语境写入成功
- **WHEN** 视觉标注、研究流程或人工输入产生符合 schema 的有效内容
- **THEN** 数据库事务保存内容及其字段来源、模型标识和生成时间，并更新语义状态

#### Scenario: 语境生成失败
- **WHEN** 生成或研究流程未配置、超时或返回无效内容
- **THEN** 系统保留最近一次有效 meme 语境，并记录可诊断的失败状态

### Requirement: 元数据更新必须原子可见
系统 MUST 以单次数据库事务提交一条 Meme 的完整语境、来源和状态更新。并发更新 MUST 防止较旧任务覆盖较新的人工或自动结果，失败事务不得留下部分字段更新。

#### Scenario: 更新事务失败
- **WHEN** 元数据更新在提交前发生校验错误、数据库错误或进程中断
- **THEN** 后续读取只能看到更新前的完整版本或更新后的完整版本，不会看到半提交内容

#### Scenario: 过期任务尝试写回
- **WHEN** 后启动的人工或自动更新已经提交，而较旧任务随后尝试基于旧版本写回
- **THEN** 系统拒绝过期写回或执行不覆盖新字段的受控合并
