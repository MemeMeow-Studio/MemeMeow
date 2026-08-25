## Purpose

为 scope-bound 文件对象与 PostgreSQL durable 记录建立单一、可恢复且 fail-closed 的存储一致性契约，确保上传、重命名、删除及任务驱动的文件副作用在失败、恢复和历史 import 场景下都不会泄露越权对象或伪造完成事实。

## ADDED Requirements

### Requirement: Storage implementation has one canonical boundary

系统 MUST 只在 `backend.persistence.storage` 提供 BlobStore 与 StorageCoordinator 的实现；兼容模块 MUST 只显式导出同一对象。资源装配 MUST 依赖 canonical 模块，canonical 模块不得反向依赖兼容 facade、HTTP 入口或 Server 专属模块。

#### Scenario: Canonical and legacy imports share identity

- **WHEN** 调用方分别从 `backend.persistence.storage` 和 `backend.database` 导入 BlobStore、StorageCoordinator
- **THEN** 两条路径返回同一个 Python 类对象，且旧调用方无需迁移即可继续运行

#### Scenario: No duplicate storage implementation exists

- **WHEN** 静态检查 persistence storage、resources 与 database facade 的导入边界和类定义
- **THEN** storage 实现只存在一个来源，canonical 模块不导入 `backend.database`、HTTP 模块或 Server 适配层

### Requirement: Scope-bound file operations are safe and durable

系统 MUST 通过绑定 scope 的 BlobStore 解析、写入、移动、隔离和删除对象；业务 key MUST 拒绝绝对路径、穿越、控制字符、内部目录名和符号链接逃逸。暂存写入 MUST 独占创建并刷新文件内容，文件落位/删除 MUST 尽力刷新目录项，移动 MUST 不覆盖已存在目标。

#### Scenario: Unsafe or cross-scope key is rejected

- **WHEN** 调用方提交绝对路径、路径穿越、`.staging`/`.quarantine` 内部键、符号链接或根目录外对象
- **THEN** 操作以稳定 DatabaseError 拒绝，且不读取、覆盖或删除目标外部对象

#### Scenario: Atomic move keeps target exclusive

- **WHEN** 同一 scope 的文件从暂存或业务 key 移动到已存在目标
- **THEN** 移动失败并保持源对象与目标对象原状，系统不得覆盖目标

### Requirement: Cross-storage operations retain durable facts and recover safely

系统 MUST 为上传、重命名和删除持久化 operation intent、scope、对象 key、SHA/size 及适用的 revision/claim/title 指纹；状态只能按合法矩阵推进。文件副作用和数据库事实无法唯一绑定时 MUST 标记 blocked/unknown_execution，不能静默删除记录、重复递增 revision 或把半提交资源列入正常查询。

#### Scenario: Upload failure is compensatable

- **WHEN** 上传已写入 durable Meme/operation 但暂存或目标文件未形成可验证对象
- **THEN** 恢复器仅在能证明没有目标副作用时补偿 Meme 并将 operation 标为 compensated；存在歧义时保持 blocked

#### Scenario: Rename recovery requires the original fact

- **WHEN** rename operation 的源/目标文件、SHA/size、Meme storage_key/revision 或任务 claim 不能形成唯一事实
- **THEN** 恢复器不移动或 finalize 文件，operation 标记 blocked，调用方获得稳定未知执行错误

#### Scenario: Delete preserves the durable deletion fact

- **WHEN** 删除已把对象移入隔离区但数据库 finalize 或隔离对象清理失败
- **THEN** 恢复器依据隔离对象身份继续收束；数据库删除事实不会被伪造回滚，无法确认时保持 blocked 并保留审计 operation

### Requirement: Fenced task-driven rename is fail-closed

任务驱动 rename MUST 在文件副作用前后复核当前 scope、Task 类型/stage/status、lease owner、claim generation、attempt、Meme SHA、storage_key、revision、标题指纹和文件身份。lease 过期、claim 重绑、人工同图改名可分别按已有稳定语义补偿或阻断；其余未知组合 MUST 保持 blocked。

#### Scenario: Expired or rebound lease cannot finalize

- **WHEN** finalize 前 Task lease 过期、owner/generation/attempt 不匹配或任务已进入终态
- **THEN** rename 不更新 Meme storage_key/revision，operation 保留 blocked/unknown_execution 事实

#### Scenario: Manual same-image replacement is compensatable only before side effect

- **WHEN** 文件移动尚未被当前 operation 证明发生，且 Meme 已被同 SHA 的人工 rename 替换
- **THEN** operation 可标为 compensated 并返回稳定 storage_key_changed；若文件副作用已发生或身份不明，则 MUST blocked

### Requirement: Legacy database exports remain compatible

`backend.database` MUST 继续导出 BlobStore、StorageCoordinator、DatabaseResources、模型、DatabaseError、Repository、engine/UoW 函数和既有常量；迁移、脚本、API、Worker 与宿主适配无需修改历史 import。公开错误不得泄露物理路径、scope namespace 或数据库凭据。

#### Scenario: Existing storage consumers keep working

- **WHEN** 现有 API、任务 Worker、迁移脚本或测试从 `backend.database` 导入 storage symbol
- **THEN** 导入成功且行为、错误码、scope 绑定和返回对象与 canonical 实现一致

#### Scenario: Missing PostgreSQL is not masked

- **WHEN** storage coordinator 的数据库环境不可用或 schema 不受支持
- **THEN** 系统明确抛出既有 DatabaseError/启动门禁错误，不静默回退到 sidecar、文件列表或伪造恢复成功
