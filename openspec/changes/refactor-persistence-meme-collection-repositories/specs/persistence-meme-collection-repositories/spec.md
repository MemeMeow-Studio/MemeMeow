## Purpose

为图片记录与合集关系提供独立、scope 绑定且可单独验证的持久化访问边界，同时保留现有调用方的导入兼容和事务事实。

## ADDED Requirements

### Requirement: Meme persistence remains scope-bound and transactional

系统 MUST 将 Meme 的读取、创建、语境更新、重命名和删除限制在构造时绑定的 scope 内；非法或跨 scope 标识不得读取或修改其他 scope 的记录。所有写操作 MUST 继续在调用方共享的事务 Session 中完成，并保留现有稳定错误码。

#### Scenario: Cross-scope Meme access is hidden

- **WHEN** 使用一个 scope 的数据访问环境读取另一个 scope 的 Meme ID 或 storage key
- **THEN** 读取返回不存在，写入不会修改另一个 scope 的记录

#### Scenario: Meme pagination and storage transition filtering remain stable

- **WHEN** 按搜索词、页码和页大小读取 Meme 列表或数量
- **THEN** 结果继续按 storage key 与 UUID 稳定排序、按原边界限制分页，并排除处于活动存储操作中间态的记录

#### Scenario: Meme write preconditions keep stable errors

- **WHEN** 创建或重命名使用非法业务 storage key、目标不存在、目标冲突、revision/SHA 已变化或任务 claim 已过期
- **THEN** 系统拒绝写入并返回原稳定数据库错误码，不静默降级

### Requirement: Collection persistence preserves scoped membership semantics

系统 MUST 将合集及成员关系限制在构造时绑定的 scope 内，并继续提供名称规范化、CRUD、分页、批量幂等成员变更、成员数量、封面和导出快照语义。合集操作 MUST 不直接移动或删除 Meme 文件。

#### Scenario: Collection names and CRUD keep contract

- **WHEN** 创建、重命名、查询或删除合集
- **THEN** 名称首尾空白、长度和同名冲突按原规则处理，跨 scope 或不存在的合集返回原错误码，成员关系仍按数据库级联事实维护

#### Scenario: Batch membership is all-or-nothing and idempotent

- **WHEN** 批量加入重复 Meme、空数组或包含越界/不存在 Meme，或幂等移除成员
- **THEN** 成功请求返回原 added/existing/total 计数，空数组或任一无效 Meme 拒绝整批，重复操作不产生重复关系

#### Scenario: Export snapshot excludes unstable storage rows

- **WHEN** 读取合集导出成员
- **THEN** 结果按加入时间和 Meme UUID 稳定排序，并排除处于 `prepared` 或 `file_applied` 存储中间态的记录

### Requirement: Legacy imports and implementation ownership remain compatible

系统 MUST 继续使历史调用方从 `backend.database` 导入 Meme 与合集 Repository；旧导出对象 MUST 与新持久化模块中的唯一实现对象相同。新 Repository 模块 MUST 不反向导入 `backend.database`，且不得复制 SearchRepository、TaskRepository、BlobStore、StorageCoordinator、schema 或 migration 实现。

#### Scenario: Existing facade imports resolve to the canonical classes

- **WHEN** 业务模块、资源装配或测试分别从旧路径和新路径导入 Meme 与合集 Repository
- **THEN** 两条路径均可用并得到同一 Python 类对象，调用方无需迁移导入

#### Scenario: Repository extraction does not alter surrounding boundaries

- **WHEN** 对新模块执行静态依赖检查并运行持久化/API/scope/任务回归
- **THEN** 新模块可独立导入，不形成 facade 循环，Search/Task/Blob/Storage/schema/migration 边界和现有行为保持不变
