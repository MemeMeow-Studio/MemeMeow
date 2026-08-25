## Purpose

为已生成的视觉向量提供独立、scope 绑定且可审查的持久化访问边界，保留模型空间和图片内容身份校验、候选资格过滤、稳定匹配排序及旧调用方兼容。

## ADDED Requirements

### Requirement: Visual vector persistence is scope- and identity-bound

系统 MUST 将视觉向量读取和写入限制在构造时绑定的 scope、模型标识、预处理版本和正维度空间内。写入 MUST 校验向量维度、有限性、非零范数、图片 SHA-256 与当前 Meme 内容一致；非法输入、缺失 Meme、跨 scope 标识或 SHA 不匹配 MUST fail-closed 并保留稳定错误语义。

#### Scenario: Valid vector is normalized and upserted for the current image

- **WHEN** 当前 scope 中存在 Meme，调用方提供有效向量、模型空间身份和与 Meme 相同的图片 SHA-256
- **THEN** 系统在共享事务中保存当前图片版本的归一化向量，并对同一模型空间的重复写入执行幂等更新

#### Scenario: Invalid or stale write is rejected

- **WHEN** 向量维度、数值有限性、范数、模型身份、Meme 标识或图片 SHA-256 任一前置条件无效
- **THEN** 系统拒绝写入并返回稳定数据库错误，不创建或覆盖其它 scope、图片版本或模型空间的记录

### Requirement: Matching uses only eligible current-scope embeddings

匹配 MUST 只读取当前 scope 中模型空间一致、向量 SHA 与 Meme 当前 SHA 一致且存储操作不处于活动中间态的候选。候选还 MUST 具有 research Agent 已完成且绑定当前图片 SHA 的可信语境；查询向量 MUST 经同样校验，默认排除请求图片自身，并按相似度降序及稳定 Meme 标识升序返回，不得混入未校验数据。

#### Scenario: Eligible candidates are ranked deterministically

- **WHEN** 当前 scope 有多个同一模型空间的有效候选，其中部分候选完成了 Agent 语境且图片仍未进入活动存储操作
- **THEN** 系统只返回合格候选，默认排除自身，并按余弦相似度降序、Meme 标识升序稳定排序且遵守结果上限

#### Scenario: Stale, cross-scope, or unready candidates are hidden

- **WHEN** 候选属于其它 scope、视觉向量 SHA 已过期、语境未 ready、provenance 不完整或图片存在活动存储操作
- **THEN** 系统排除该候选，不泄露其向量、图片或语境事实

### Requirement: Legacy facade keeps one implementation identity

系统 MUST 继续支持从 `backend.database` 导入视觉向量 repository 和向量校验函数；旧路径导出 MUST 分别与持久化 repository 模块中的同一 Python 类/函数对象一致。canonical repository 模块 MUST 不在顶层导入 `backend.database`，也不得复制任务、反向图片、callback、文件存储、资源协调、schema 或 migration 实现。

#### Scenario: Existing environment and callers keep imports and shared session

- **WHEN** 业务模块、`DataEnvironment` 或测试通过旧路径构造 repository，同时另一路径导入 canonical repository
- **THEN** 两条路径解析为同一对象，构造参数、scope 和共享 Session 行为不变，调用方无需迁移

#### Scenario: Surrounding boundaries remain outside the extraction

- **WHEN** 对新模块执行静态实现唯一性、依赖方向和边界回归检查
- **THEN** 视觉推理/HTTP、任务、反向图片、callback、BlobStore、StorageCoordinator、schema/migration、frontend 和其它 active change 均未被移动或重复实现
