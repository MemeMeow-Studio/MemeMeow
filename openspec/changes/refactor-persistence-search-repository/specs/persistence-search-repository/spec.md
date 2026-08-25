## Purpose

为 generation、迁移控制面以及文本 embedding 查询提供独立的 scope 绑定持久化访问边界，并保留 `backend.database` 历史兼容导出。

## ADDED Requirements

### Requirement: SearchRepository remains scope-bound and fail-closed

系统 MUST 将 SearchRepository 的 generation、head、migration state、legacy embedding 和 incremental text embedding 读写限制在构造时绑定的 scope 内。非法维度、零范数、损坏 embedding、非法 migration 参数、过期 claim、跨 scope 标识和不完整 generation MUST 按既有错误码或拒绝结果 fail-closed，不得静默扩大查询范围或混读两套来源。

#### Scenario: Generation and claim writes stay within the bound scope

- **WHEN** 使用一个 scope 的 SearchRepository 创建、写入、激活或失败处理 generation，并提供可选 Worker claim
- **THEN** 所有 generation、head、item 和 claim 查询均带当前 scope；过期或不匹配 claim 返回 `claim_expired`，跨 scope 或无效 generation 不会被修改

#### Scenario: Migration state is epoch- and model-safe

- **WHEN** 开始增量回填、记录进度或切换 `incremental_only`
- **THEN** model、count、epoch、legacy generation 和完成度遵循既有校验；旧 epoch 不能回拨新 epoch，未完成回填不能切换，迁移状态存在时不能恢复读取未冻结的 head

### Requirement: Text embedding source selection and ranking remain stable

系统 MUST 继续在 legacy generation 与 incremental text embedding 之间选择唯一查询来源，并保留 SHA、revision、metadata、语境、storage key、scope、维度和状态校验。查询 MUST 按余弦得分降序、`meme_id` 字符串升序稳定排序，并保留 limit 边界、`cache_not_ready`、`embedding_dimensions_mismatch`、`embedding_zero_norm` 等错误语义。

#### Scenario: Migration mode prevents mixed-source reads

- **WHEN** `SearchRepository.query` 根据当前 migration state 选择查询来源
- **THEN** `incremental_only` 只查询有效 incremental rows，backfill/legacy 只查询冻结且逐条校验通过的 legacy rows，任何一次查询不得调用另一来源

#### Scenario: Corrupt or stale rows are excluded before ranking

- **WHEN** text embedding 的 image SHA、Meme revision、metadata hash、语境、业务 storage key、维度、状态或数值范数不匹配
- **THEN** 该行被排除，剩余结果仍按原稳定排序返回；没有有效结果时返回 `cache_not_ready`，不返回未校验数据

### Requirement: Legacy facade and implementation ownership remain compatible

系统 MUST 继续支持从 `backend.database` 导入 SearchRepository；旧导出对象 MUST 与 `backend.persistence.repositories.search.SearchRepository` 是同一个 Python 类对象。新模块 MUST 不在顶层导入 `backend.database`，且不得复制 VisualEmbeddingRepository、TaskRepository、ReverseImageUsageRepository、BlobStore、StorageCoordinator、schema 或 migration 实现。

#### Scenario: Existing resources and callers keep their imports

- **WHEN** 业务模块、`DataEnvironment` 或测试从旧路径构造 SearchRepository，并与新路径类对象比较
- **THEN** 构造参数和共享 Session/scope 行为不变，旧路径与新路径解析为同一实现，调用方无需迁移

#### Scenario: Surrounding persistence boundaries are untouched

- **WHEN** 对新模块执行静态依赖、类实现唯一性和边界回归检查
- **THEN** 视觉/任务/反向图片 Repository、文件存储、资源协调、schema/migration、HTTP、frontend 和 active change 文件均未被移动或重复实现
