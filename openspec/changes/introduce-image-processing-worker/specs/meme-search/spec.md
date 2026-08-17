## MODIFIED Requirements

### Requirement: 系统必须执行受有效产物保护的语义检索
系统 MUST 使用当前 scope 中与当前 Meme 图片 SHA-256、metadata hash 和 embedding 模型匹配的有效单图文本向量执行查询，并返回不超过请求数量的可访问图片引用。当前 scope 没有任何有效文本向量时，系统 MUST 返回明确的索引未就绪错误，不得返回跨 scope 或过期内容的候选结果。单图文本向量生成、更新或失效 MUST 原子影响该图片的搜索资格，不得等待全库 generation 激活。生成语义文本时 MUST 从 PostgreSQL 中非空的 `title`、`summary`、`subjects`、`visible_text`、已确认 `references`、非空 `meaning` 和 `keywords` 按固定格式构造；`search_queries`、`uncertainties` 和 `source_urls` MUST 不得进入 embedding。

#### Scenario: 有效单图向量存在时返回结果
- **WHEN** 客户端提交非空查询且当前 scope 至少存在一个有效文本向量
- **THEN** 系统按相关性返回最多 `n_results` 个当前 scope 的图片引用，且不返回重复引用

#### Scenario: 索引未就绪
- **WHEN** 客户端提交查询但当前 scope 尚无有效文本向量
- **THEN** 系统返回 `503`，错误标识为 `cache_not_ready`，且不返回部分检索结果

#### Scenario: 单图向量更新期间查询
- **WHEN** 当前 scope 已有可用文本向量且某张图片正在生成新的文本向量
- **THEN** 系统继续使用其它有效向量，并仅在该图片的新向量原子提交后将其纳入结果

#### Scenario: 空查询
- **WHEN** 客户端提交空白或缺失的查询文本
- **THEN** 系统返回 `400`，错误标识为 `invalid_query`

#### Scenario: 数据库语境驱动单图向量生成
- **WHEN** Meme 具有 `partial` 或 `ready` 的可用语境且系统为该图片生成文本向量
- **THEN** 系统仅使用已填充的白名单事实字段构造语义文本，并保存该图片的 metadata hash、embedding 模型和内容指纹

#### Scenario: 不可用语境不得回退文件名
- **WHEN** Meme 为 `pending`、`repair_required` 或白名单语义文本为空
- **THEN** 系统不为该 Meme 写入可查询的文本向量，不使用文件名或不确定内容生成 embedding

#### Scenario: 全部 Meme 都不可索引
- **WHEN** 当前 scope 没有任何具有有效语义文本的 Meme
- **THEN** 系统保持索引未就绪，且不以空结果覆盖已有有效单图向量

#### Scenario: embedding 维度不匹配
- **WHEN** embedding 服务返回的向量维度不是系统配置的固定维度
- **THEN** 系统拒绝写入该图片文本向量并使该图片处理阶段失败，已有其它有效向量继续可用

## ADDED Requirements

### Requirement: 单图文本向量必须随图片和语境失效
系统 MUST 在图片 SHA-256、可 embedding 的语境字段、metadata hash 或 embedding 模型任一变化后，使不再匹配的单图文本向量失去搜索资格。显式全库 `cache_generation` 可以继续作为迁移、修复和重建工具，但其创建、完成或失败 MUST NOT 覆盖符合当前单图有效性条件的文本向量。

#### Scenario: Agent 语境发生有效变化
- **WHEN** 当前图片版本的可 embedding 语境字段变化，导致 metadata hash 改变
- **THEN** 旧文本向量不再参与搜索，直到新 hash 的文本 embedding 原子提交

#### Scenario: 执行显式全库重建
- **WHEN** 维护者显式提交 scope 级 `cache_generation`
- **THEN** 该维护任务按其自身契约执行，且不删除、覆盖或降低当前有效单图文本向量的搜索资格

### Requirement: 索引迁移期间一次查询只能选择一种来源
系统 MUST 为每个 scope 持久化增量向量迁移状态，并在一次查询中只选择一种来源：已验证的旧 generation 或当前有效单图向量。系统 MUST NOT 将两种来源混合排序。旧 generation 只有在逐条通过当前 scope、Meme、图片 SHA-256、metadata hash、embedding 模型、维度和图片可访问性校验时才可作为回退；无法安全校验的条目 MUST 被排除。

#### Scenario: scope 仍在迁移
- **WHEN** scope 处于增量向量回填状态且尚未完成原子切换
- **THEN** 一次查询选择受迁移 epoch 保护的单一旧 generation 回退或单一增量来源，不混合两个来源的候选结果

#### Scenario: scope 完成切换
- **WHEN** scope 的增量向量回填和校验完成并原子切换到 `incremental_only`
- **THEN** 后续查询只读取当前有效单图向量，旧 generation 不再作为日常回退

#### Scenario: 旧 generation 条目无法证明仍有效
- **WHEN** 旧 generation 中某条记录无法证明当前 metadata hash、模型版本或图片文件仍匹配
- **THEN** 系统排除该条；若 scope 无法安全提供任何来源，则返回 `503/cache_not_ready`
