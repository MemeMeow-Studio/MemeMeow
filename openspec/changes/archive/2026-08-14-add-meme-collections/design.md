## Context

数据库重构已使 PostgreSQL 成为结构化业务数据的权威来源，并以稳定 UUID 区分 Meme 身份与可变 `storage_key`。`DataEnvironment` 在请求或任务期间创建一个共享 Session 的 scope-bound repository 组合，进程级 Engine、连接池和 BlobStore 则保持共享。开源入口固定绑定 `local`，客户端不能选择 scope。

`remove-image-directories` change 会先删除用户可见目录、把业务 `storage_key` 收敛为扁平文件名，并通过 `0003_flat_meme_storage` 固化 migration 基线。合集 change 以该结果为前置条件，不再承担目录兼容或 migration 基线修复。

图片库已有多选交互，但当前选择仅允许元数据可重试的图片；新增合集工作流需要把通用选择和重试资格分离。

## Goals / Non-Goals

**Goals:**

- 复用现有 scope、事务和稳定 Meme 身份建立轻量逻辑合集。
- 在数据库约束与 repository 两层阻止跨 scope 成员关系。
- 让合集成员变化不触发图片文件操作或向量重建。
- 在 `0003_flat_meme_storage` 基础上以前向 migration 安装合集 schema。
- 在现有操作型工作台中提供完整且可访问的合集管理流程。

**Non-Goals:**

- 不让公共核心感知用户账户，也不实现共享授权、团队或公开链接。
- 不实现嵌套、手工排序、独立封面、描述、标签、导入导出或合集删除图片。
- 不为每个合集创建 DataEnvironment、连接池、BlobStore 或搜索 generation。
- 不恢复任何用户可见目录；扁平图片库表达资产全集，合集是唯一组织方式。
- 不在本 change 增加“限定合集搜索”；成员关系保持可供后续查询 join 使用。

## Decisions

### 1. 使用合集表和多对多成员表

新增 `meme_collections`：

```text
id uuid primary key
scope_id text not null
name varchar(100) not null
created_at / updated_at timestamptz not null
unique(scope_id, id)
unique(scope_id, name)
```

新增 `meme_collection_items`：

```text
scope_id text not null
collection_id uuid not null
meme_id uuid not null
added_at timestamptz not null
primary key(scope_id, collection_id, meme_id)
foreign key(scope_id, collection_id) -> meme_collections(scope_id, id) on delete cascade
foreign key(scope_id, meme_id) -> memes(scope_id, id) on delete cascade
index(scope_id, collection_id, added_at, meme_id)
```

复合外键使数据库本身无法把某 scope 的合集与另一 scope 的 Meme 关联。成员使用稳定 `meme_id`，所以重命名图片无需更新关系。删除 Meme 或合集都只级联删除关系行。

不把成员 ID 写入合集或 Meme 的 JSONB，因为 JSONB 无法提供复合外键、成员唯一性、可靠分页和并发事务。也不复用 `TaskBatch`，因为任务批次是执行状态，不是用户持久内容。

首版不增加 `position`。详情按 `added_at ASC, meme_id ASC` 稳定排序，封面取第一条仍有效成员。等出现明确的拖拽排序或导出顺序需求时，再以前向 migration 增加稀疏位置字段，避免现在承担重排和并发排序复杂度。

### 2. 合集 repository 绑定 scope 并复用当前 Unit of Work

`CollectionRepository(session, scope)` 加入 `DataEnvironment.collections`，与 Meme、Search 和 Task repository 使用同一 Session。repository 的公开方法不接受 `scope_id`；读取、更新、删除、计数和关联校验都自动限定构造时的 scope。

创建 DataEnvironment 只分配少量 Python 包装对象和一个按需获取连接的 Session，不加载合集或复制重型资源。对象生命周期随请求或任务结束，因此内存随并发请求量而不是用户、Meme 或合集总数增长。

批量加入先锁定当前 scope 的合集，再对去重后的 Meme ID 做一次集合查询。只有查询结果与请求集合完全一致才批量写入缺失关系；否则抛出异常使 Unit of Work 回滚。数据库主键处理并发重复加入，repository 将唯一冲突重新读取为幂等成功。

### 3. 提供独立合集资源 API，但不暴露 scope

API 使用 `collections`，避免中文“表情包”既可能指单张 Meme 又可能指一组图片的歧义：

```text
GET    /collections?page=&page_size=
POST   /collections
GET    /collections/{collection_id}?page=&page_size=
PATCH  /collections/{collection_id}
DELETE /collections/{collection_id}
POST   /collections/{collection_id}/items
DELETE /collections/{collection_id}/items/{meme_id}
```

创建和更新只接受 `name`；成员写入只接受 `meme_ids`。任何请求体和查询参数都不接受 `scope_id` 或 `user_id`。当前适配器从服务端固定得到 `ScopeContext("local")`，未来闭源适配器可以从已认证会话得到另一可信 scope，而无需修改合集领域 API。

列表返回 `items/total/page/page_size`，每项包含 `collection_id/name/member_count/cover_media_url/timestamps`。详情在同一响应中包含合集字段和分页 `members`；成员字段复用当前图片库的稳定 `meme_id`、文件信息、媒体 URL 和状态表达。所有 UUID 参数在边界统一校验；不存在和跨 scope 资源都映射为同一 404，重名映射为 409，无效名称或空成员数组映射为 422。

批量加入响应包含 `added_count`、`existing_count` 和最终 `member_count`。删除成员采用幂等语义：合集存在时，即使关系已不存在也返回成功；合集不存在仍返回 404。这便于界面在重复操作或网络重试后收敛。

不新增 `/collections/{id}/search`。未来若需要限定合集搜索，应在唯一 `POST /search` 增加可选 `collection_id`，在当前 active generation 查询中 join 成员关系；合集成员变化本身不改变 Meme 语义、revision 或向量。

### 4. 将图片库选择改为通用选择，再按动作判断资格

图片库的选择框允许选择任意当前可见 Meme。选择工具条显示稳定的已选数量，并提供“加入合集”；“重试选中”只统计并处理所选项中满足现有可重试条件的 Meme，按钮文案展示其实际数量。选择状态以稳定 `meme_id` 为键，翻页、筛选变化或操作完成时使用明确规则清理，避免把不可见旧选择误提交。

“加入合集”打开一个专注的对话框，使用原生可识别的单选列表选择已有合集，并提供“新建合集”命令；提交期间锁定重复操作，成功后显示文字反馈并退出当前选择。空合集列表直接呈现新建表单。桌面对话框保持单层内容面，移动端变为全屏操作面，不使用卡片嵌套。

侧栏新增“合集”。合集列表延续图片库的分隔行结构：小型真实封面、名称、成员数量和更新时间为主扫描信息，操作放入紧凑菜单。点击行进入同一工作区内的详情视图，提供返回、重命名、删除和成员移除；删除合集使用确认对话框，并明确说明图片不会删除。空列表和空合集分别提供创建合集、前往图片库添加图片的直接动作。

### 5. 合集 migration 显式依赖扁平存储 revision

`remove-image-directories` 负责固化 `0001` 并创建 `0003_flat_meme_storage`。本 change 随后新增 `0004_meme_collections`，以 `0003_flat_meme_storage` 为 `down_revision`，只创建合集及成员表、约束和索引，并更新安装标记 revision。

不得使用 `IF NOT EXISTS` 或 `checkfirst` 掩盖 revision 顺序错误。测试必须从空 PostgreSQL 执行 `upgrade head`，并覆盖带有现有 Meme 的 `0003 -> 0004` 升级；应用前必须先完成和验收 `remove-image-directories`，两个 change 不并行实施或反向归档。

## Risks / Trade-offs

- [风险] 未先应用扁平存储 change 就实施合集，导致 migration 父级或 API 假设不成立 → `0004` 显式依赖 `0003_flat_meme_storage`，任务和验收先检查前置 change 已完成。
- [风险] 同名并发创建或重复批量加入触发唯一冲突 → 以数据库唯一约束为最终裁决，并在 repository 中映射为稳定的冲突或幂等结果。
- [风险] 在 API 层遗漏 scope 条件造成越界读取 → API 不直接拼接合集 SQL，全部通过 scope-bound repository；使用两个 scope 的 PostgreSQL 集成测试覆盖读取和关联攻击。
- [风险] 详情页逐项读取图片状态形成 N+1 查询 → repository 使用聚合与批量查询返回分页数据，媒体 URL 由稳定 ID 构造，文件状态只在确有需要时批量解析。
- [权衡] 不提供手工封面和排序使首版能力较克制 → 确保模型简单且删除行为可靠，后续可以在不改变成员身份的情况下扩展。
- [权衡] 精确重名约束允许大小写不同的拉丁名称并存 → 避免现在引入语言相关归一化；界面展示服务器的冲突结果，后续若有实际混淆再单独设计规范化策略。

## Migration Plan

1. 完成并验收 `remove-image-directories`，确认数据库位于 `0003_flat_meme_storage` 且图片库全部为扁平业务 key。
2. 增加合集 ORM 和 `0004_meme_collections`，在临时数据库分别验证空库完整升级与 `0003` 原有 Meme 数据升级。
3. 部署代码前运行 `alembic upgrade head`，再由应用初始化更新并验证安装标记。
4. 合集表初始为空，无需导入目录，不移动图片或重建向量。
5. 若应用发布需要回退，旧版本代码可忽略新增表；数据库保持前向 schema，不执行破坏性 downgrade。修正版通过新的前向 revision 发布。
