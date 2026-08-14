## Context

图片文件仍由 scope-bound BlobStore 保存，PostgreSQL `memes.storage_key` 记录图片根下的 POSIX 相对路径。现有 API 和前端允许选择、创建和浏览目录，上传也接受目标目录；但当前数据没有子目录，且合集将成为用户可见的组织模型。

内部存储还使用 `.staging` 和 `.quarantine` 完成可恢复上传、重命名和删除。这些是 BlobStore 的实现命名空间，不属于业务 `storage_key`，不能因为删除目录能力而取消。

首个 Alembic revision 当前从运行时 ORM 的 `Base.metadata` 动态建表。任何未来模型都会污染历史 migration，因此本 change 必须先固定 migration 基线，才能安全增加扁平约束和后续合集表。

## Goals / Non-Goals

**Goals:**

- 从领域、API 和界面彻底移除用户可见目录。
- 让每个业务 `storage_key` 都是当前 scope 图片根下的单个安全文件名。
- 对已有扁平数据原地升级，不移动图片或改变 Meme 身份。
- 对意外存在的非扁平数据明确失败，不进行猜测性修复。
- 保持跨存储恢复流程和内部临时命名空间可用。

**Non-Goals:**

- 不保留目录端点、目录参数、兼容响应或弃用期。
- 不递归扫描、搬平、重命名或导入子目录文件。
- 不增加独立 `display_name`；文件名继续同时承担当前用户可见名称和内部扁平存储键。
- 不改变稳定 `meme_id`、scope 设计、图片格式、批量部分成功或自动命名语义。
- 不在本 change 实现合集。

## Decisions

### 1. 业务 storage key 必须是单个安全文件名

合法业务 `storage_key` 必须满足：

- 非空且长度不超过现有列限制；
- 不等于 `.` 或 `..`；
- 不包含 `/`、反斜线、NUL 或控制字符；
- 不以 `.staging`、`.quarantine` 等内部保留名称表达业务资源；
- 清理后文件名和扩展名符合现有上传、重命名规则。

数据库新增 CHECK 约束作为最终防线，repository 与所有业务入口使用同一校验函数尽早返回稳定错误。BlobStore 仍保留通用相对 key 能力，因为存储协调器需要访问内部 `.staging/<token>` 和 `.quarantine/<token>`；业务校验不能错误套用到内部操作 key。

不增加 `display_name`。当前文件名本来就是用户编辑和筛选的名称，存储位于每 scope 独立根目录，同 scope 内唯一约束足以支撑扁平模型。拆出显示名会增加同步、重名和自动命名语义，但当前没有独立价值。

### 2. 删除目录 API，而不是保留无操作兼容层

删除 `GET /images/directories` 和 `POST /images/directories`。`GET /images` 只接受 `search/page/page_size`，响应为 `items/total/page/page_size`；每个图片项删除 `directory` 字段，顶层删除 `directory/directories`。提交未知 `directory` 参数必须由请求边界拒绝，不能静默忽略。

上传不再声明 `directory` Form 参数，目标始终是绑定 scope 的 BlobStore 根。为确保“提交旧字段返回错误”，上传解析需要显式检查 form keys 或采用禁止额外字段的受控解析，避免框架默认忽略额外 multipart 字段。批量内部仍逐文件处理，但请求级契约错误在读取和写入文件前失败。

重命名始终以 Meme 当前扁平 key 的父级根作为目标，不接受路径。响应删除 `directory`。旧端点直接变为 404，不添加迁移错误或别名，因为用户明确选择不兼容。

### 3. 图片列表使用数据库扁平分页

`MemeRepository.list` 删除 `directory` 过滤能力，按文件名筛选并在 SQL 层分页、计数和确定性排序，不再先加载当前 scope 的全部 Meme 后由 Python 过滤。列表继续排除存在活动 storage operation 的 Meme。

默认按当前 `storage_key` 的稳定、大小写可预测顺序排列，以 `meme_id` 作为并列顺序。合集 change 可以独立使用加入时间排序，不影响图片库。

### 4. 前端删除全部目录状态

删除目录选择器、新目录输入与按钮、上传目标目录以及相关响应状态。图片库标题和空状态改为“图片库还没有图片”，筛选和刷新保留。上传 API 只接收文件与自动命名选项。

不保留隐藏目录状态，也不把“全部图片”显示成一个名为“根目录”的选项，因为这仍会暗示存在目录层级。合集上线后，导航中的“图片库”与“合集”分别表达资产全集和逻辑组织。

### 5. 固化 migration 基线并以前向 revision 加入约束

先把 `0001_postgres_scoped` 改写成自包含的首版 schema 定义，移除运行时 `Base.metadata` 导入；`0002_batch_sealed` 保持原职责。新增 `0003_flat_meme_storage`：

1. 在任何 DDL 前查询业务 Meme 和活动 storage operation 的业务字段；发现包含路径结构的业务 key 时抛出带数量的明确 migration 错误，不修改数据。字段分类固定为：upload 的 `target_key`、rename 的 `source_key/target_key`、delete 的 `source_key` 是业务 key；`staging_key` 与 delete 的 `.quarantine/` target 是内部 key并显式豁免。
2. 为 `memes.storage_key` 增加扁平 CHECK 约束。
3. 更新安装标记的 schema revision。

`storage_operations` 的 staging/quarantine key 不能应用扁平约束；正常业务字段则由协调器保证来自合法 Meme key。应用启动就绪检查同时验证数据库 Meme key，并只读递归检查图片根中的受支持图片；`.staging`、`.quarantine` 及其受控内容在遍历入口即排除。发现嵌套业务图片或记录/文件不一致时拒绝就绪，不自动修复。测试覆盖已有扁平数据从 `0002` 升级、非扁平数据拒绝、内部 key 豁免，以及空库顺序执行 `0001 -> 0002 -> 0003`。

`add-meme-collections` 必须以 `0003_flat_meme_storage` 为 migration 父级，再创建 `0004_meme_collections`。两个 change 在规划和实施中保持明确先后，不共享 revision 编号。

## Risks / Trade-offs

- [风险] 实际数据库或文件系统中存在未发现的子目录 → migration 检查数据库业务字段，应用启动只读扫描 BlobStore；任一发现反例都拒绝继续并要求人工处理，不自动移动。
- [风险] FastAPI 默认忽略未知 query 或 multipart 字段，导致旧客户端看似成功 → 为涉及 breaking 字段的路由增加显式未知字段契约测试和请求边界校验。
- [风险] 固化 `0001` 遗漏旧约束或索引 → 从空 PostgreSQL 完整升级并比较关键 schema，同时测试现有 `0002` 升级。
- [风险] 业务 filename 校验误伤内部恢复 key → 分离业务 key 与 BlobStore 内部 key 校验函数，恢复测试覆盖 staging 和 quarantine。
- [权衡] 文件名兼任展示名称与存储键，使重命名仍需跨数据库和文件系统协调 → 当前 `StorageCoordinator` 已提供恢复协议；现阶段避免新增无必要实体更简单。
- [权衡] 不兼容目录意味着存在旧数据时必须人工清理或重建 → 当前确认没有子目录，换取长期模型和 API 的彻底简化。

## Migration Plan

1. 在开发和部署环境执行与应用启动相同的只读预检，确认不存在业务子目录或未登记的嵌套图片；若存在则停止，不自动修复。
2. 固化 `0001` 并验证空库升级至 `0002` 的 schema 等价性。
3. 部署删除目录的后端和前端代码前，运行 `0003_flat_meme_storage`；已有扁平 Meme 和文件保持原位。
4. 启动应用后验证列表、上传、重命名、删除、任务、媒体读取和恢复流程只使用扁平业务 key。
5. 不执行破坏性 downgrade；需要回退应用时，旧代码仍可读取根目录数据，但已删除端点和客户端契约不保证兼容。后续修复使用新的前向 revision。
