## MODIFIED Requirements

### Requirement: 系统必须执行受缓存保护的语义检索
系统 MUST 使用当前 scope 已激活的数据库向量索引执行查询，并返回不超过请求数量的可访问图片引用。当前 scope 没有可用索引时，系统 MUST 不执行不完整检索，并返回明确的索引未就绪错误。查询不得返回其他 scope 的候选结果。生成索引时 MUST 从 PostgreSQL 中非空的 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、非空 `meaning` 和 `keywords` 按固定格式构造语义文本；`search_queries`、`uncertainties` 和 `source_urls` MUST 不得进入 embedding。

#### Scenario: 缓存就绪时返回结果
- **WHEN** 客户端提交非空查询且当前 scope 的向量索引已激活
- **THEN** 系统按相关性返回最多 `n_results` 个当前 scope 的图片引用，且不返回重复引用

#### Scenario: 缓存未就绪
- **WHEN** 客户端提交查询但当前 scope 尚无已激活索引
- **THEN** 系统返回 `503`，错误标识为 `cache_not_ready`，且不返回部分检索结果

#### Scenario: 索引刷新期间查询
- **WHEN** 当前 scope 已有可用索引且新一代索引正在生成
- **THEN** 系统继续使用已激活索引，直到新索引完整生成并原子切换

#### Scenario: 空查询
- **WHEN** 客户端提交空白或缺失的查询文本
- **THEN** 系统返回 `400`，错误标识为 `invalid_query`

#### Scenario: 数据库语境驱动索引生成
- **WHEN** Meme 具有 `partial` 或 `ready` 的可用语境且系统生成索引
- **THEN** 系统仅使用已填充的白名单事实字段构造语义文本，并保存文档、元数据和图片内容指纹

#### Scenario: 不可用语境不得回退文件名
- **WHEN** Meme 为 `pending`、`repair_required` 或白名单语义文本为空
- **THEN** 系统跳过该 Meme 并报告其索引状态，不使用文件名或不确定内容生成 embedding

#### Scenario: 全部 Meme 都不可索引
- **WHEN** 当前 scope 没有任何具有有效语义文本的 Meme
- **THEN** 索引生成任务明确失败或保持索引未就绪，且不以空 generation 替换已有激活索引

#### Scenario: embedding 维度不匹配
- **WHEN** embedding 服务返回的向量维度不是系统配置的固定维度
- **THEN** 系统拒绝写入并使本次 generation 失败，已有激活索引继续可用

### Requirement: 检索结果必须稳定且可去重
系统 MUST 按向量相关性降序返回当前 scope 的结果；相关性相同的结果 MUST 使用稳定 `meme_id` 作为次级排序键。系统 MUST 去除同一 Meme 的重复引用，并 MUST 在返回前确认 Meme 记录和图片文件仍然可访问。

#### Scenario: 相同查询重复执行
- **WHEN** 已激活索引、Meme 记录和图片文件未发生变化且客户端重复提交相同查询
- **THEN** 返回结果的顺序和内容保持一致

#### Scenario: 结果图片不可访问
- **WHEN** 候选 Meme 已删除、指纹失效或其图片文件不可访问
- **THEN** 系统跳过该候选，继续处理其他候选，不返回失效引用

#### Scenario: 相同分数的候选
- **WHEN** 多个候选具有相同相关性分数
- **THEN** 系统按 `meme_id` 稳定排序，不依赖可变文件路径决定顺序
