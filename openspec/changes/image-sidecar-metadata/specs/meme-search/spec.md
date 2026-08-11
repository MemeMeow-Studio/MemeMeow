## MODIFIED Requirements

### Requirement: 系统必须执行受缓存保护的语义检索
系统 MUST 使用已生成的图片检索缓存执行查询，并返回不超过请求数量的可访问图片引用。缓存不存在或尚未完成时，系统 MUST 不执行不完整检索，并返回明确的缓存未就绪错误。缓存生成时 MUST 从 `summary`、`subjects`、`visible_text`、已确认 `references`、非空 `meaning` 和 `keywords` 按固定格式构造语义文本；`search_queries`、`uncertainties` 和 `source_urls` MUST 不得进入 embedding。元数据发生影响上述字段的有效变化后，旧索引 MUST 被视为需要重建。

#### Scenario: 缓存就绪时返回结果
- **WHEN** 客户端提交非空查询且缓存已就绪
- **THEN** 系统按相关性返回最多 `n_results` 个图片引用，且不返回重复的图片引用

#### Scenario: 缓存未就绪
- **WHEN** 客户端提交查询但检索缓存不存在或正在生成
- **THEN** 系统返回 `503`，错误标识为 `cache_not_ready`，且不返回部分检索结果

#### Scenario: 空查询
- **WHEN** 客户端提交空白或缺失的查询文本
- **THEN** 系统返回 `400`，错误标识为 `invalid_query`

#### Scenario: meme 语境驱动缓存生成
- **WHEN** 图片具有可用的 meme 语境且客户端触发缓存生成
- **THEN** 系统仅使用字段白名单构造语义文本并生成向量，将所用字段版本或内容指纹写入索引记录

#### Scenario: 研究辅助字段不得污染索引
- **WHEN** 图片 sidecar 含有检索查询、不确定项或来源 URL
- **THEN** 系统不将这些字段写入语义文本或 embedding 输入

#### Scenario: 部分语境可安全参与索引
- **WHEN** 图片 sidecar 为 `partial` 且包含可由画面支持的白名单字段
- **THEN** 系统只使用已填充的画面事实字段构造语义文本，不得加入未经确认的引用或含义

#### Scenario: 元数据尚未完成时回退
- **WHEN** 图片 sidecar 为 `pending` 或待修复但图片文件仍然有效
- **THEN** 系统按明确的文件名回退策略处理该图片，报告其元数据状态，并不得把不确定内容当作检索事实
