## Purpose

为 MemeMeow 的公共核心提供统一、可验证的数据范围和 PostgreSQL 权威存储契约，使单用户开源部署与未来的多用户宿主适配层能够复用同一套业务能力，同时避免身份系统进入公共核心。

## ADDED Requirements

### Requirement: 结构化业务数据必须由 PostgreSQL 权威保存
系统 MUST 将 Meme 记录、meme 语境、检索向量和任务状态保存到 PostgreSQL，并 MUST 在数据库不可用或 schema 版本不受支持时拒绝启动业务服务。系统不得静默回退到 sidecar、搜索缓存或任务 JSON。

#### Scenario: 数据库和 schema 就绪
- **WHEN** 服务启动且 PostgreSQL 可连接、pgvector 可用、schema 版本符合当前应用要求
- **THEN** 系统完成数据访问初始化并允许业务请求进入

#### Scenario: 数据库不可用
- **WHEN** 服务启动时 PostgreSQL 不可连接、pgvector 缺失或 schema 版本不兼容
- **THEN** 系统明确报告启动失败，不接受可能写入其他存储的业务请求

### Requirement: 所有持久化访问必须绑定数据范围
系统 MUST 在执行任何结构化数据读取、写入、搜索或任务操作前绑定一个不可为空的 `scope_id`，且一个 scope 的操作不得读取、修改、关联或返回其他 scope 的记录。客户端不得通过普通业务参数任意指定或覆盖 `scope_id`。

#### Scenario: 开源版本访问本地数据
- **WHEN** 开源版本处理任意业务请求
- **THEN** 后端在内部将请求绑定到固定 `scope_id="local"`，客户端无需也不能提交 scope 参数

#### Scenario: 跨 scope 资源标识
- **WHEN** 当前 scope 使用属于另一 scope 的 Meme、任务或其他资源标识发起读取或修改
- **THEN** 系统返回资源不存在或无权访问的结果，且不泄露目标资源是否存在

### Requirement: Meme 必须使用与路径分离的稳定身份
系统 MUST 为每张图片分配不可变且全局唯一的 `meme_id`。重命名、目录移动、语境更新和检索索引刷新不得改变 `meme_id`；相对存储路径 MUST 仅作为可变的物理定位属性，并在同一 scope 内保持唯一。

#### Scenario: 图片重命名
- **WHEN** 用户成功重命名一张 Meme 图片
- **THEN** 系统更新其存储路径和展示文件名，但返回与重命名前相同的 `meme_id`

#### Scenario: 相同路径位于不同 scope
- **WHEN** 两个 scope 分别保存相同相对路径的图片
- **THEN** 系统为它们维护独立的 Meme 记录和物理文件命名空间，任一 scope 的操作不读取或改变另一 scope 的文件

### Requirement: 文件存储必须绑定数据范围
系统 MUST 通过已绑定 scope 的文件存储边界解析、写入、移动和删除图片，物理对象键 MUST 包含不可由客户端覆盖的 scope 命名空间。开源 `local` scope MUST 保持现有图片根目录作为其命名空间，未来其他 scope 不得与之共享同一物理对象键。

#### Scenario: 两个 scope 使用相同逻辑路径
- **WHEN** 两个 scope 分别保存逻辑路径为 `cats/a.png` 的图片
- **THEN** 系统将其解析到不同物理对象，删除或重命名任一图片不影响另一图片

#### Scenario: 客户端伪造 scope 路径前缀
- **WHEN** 客户端在目录名、文件名或资源参数中提交其他 scope 的物理前缀
- **THEN** scope-bound 文件存储拒绝该路径且不访问目标文件

### Requirement: 跨数据库和文件存储的变更必须可恢复
系统 MUST 将 PostgreSQL 作为资源是否存在及其归属的权威来源，并 MUST 对上传、重命名和删除这类同时修改数据库与文件存储的操作提供补偿或恢复记录。失败操作不得向客户端报告成功，也不得留下会被正常列表或搜索返回的半提交资源。

#### Scenario: 文件操作失败
- **WHEN** 数据库变更准备完成但文件写入、移动或删除失败
- **THEN** 系统回滚或补偿数据库变更，返回明确失败结果，原有可用资源保持可访问

#### Scenario: 进程在跨存储操作中断
- **WHEN** 服务在数据库和文件存储尚未完成一致提交时中断
- **THEN** 后续恢复流程识别未完成操作并完成或回滚，不将中间状态视为正常资源
