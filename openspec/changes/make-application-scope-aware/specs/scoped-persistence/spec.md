## MODIFIED Requirements

### Requirement: 所有持久化访问必须绑定数据范围

系统 MUST 在执行任何结构化数据读取、写入、搜索或任务操作前绑定一个不可为空的 scope_id，且一个 scope 的操作不得读取、修改、关联或返回其他 scope 的记录。公共业务入口 MUST 使用可信 request scope；后台任务和可信内部 task callback MUST 使用持久 Task.scope_id 或有效 claim。客户端不得通过普通业务参数任意指定或覆盖 scope_id。

#### Scenario: 开源版本访问本地数据

- **WHEN** 开源版本处理任意业务请求
- **THEN** 开源模块级入口通过显式注入的 LocalScopeResolver("local") 将请求绑定到固定 scope_id="local"
- **AND** 客户端无需也不能提交 scope 参数

#### Scenario: 宿主适配层提供可信 request scope

- **WHEN** 宿主适配层为请求注入经过验证的非 local scope
- **THEN** 所有该请求的 repository、文件存储和服务操作使用该 scope
- **AND** 公共核心不要求创建用户、账户或鉴权实体

#### Scenario: 跨 scope 资源标识

- **WHEN** 当前 scope 使用属于另一 scope 的 Meme、任务或其他资源标识发起读取或修改
- **THEN** 系统返回资源不存在或无权访问的结果
- **AND** 不泄露目标资源是否存在

#### Scenario: scope 缺失

- **WHEN** 业务操作尚未获得有效 request scope，或后台任务缺失有效的持久 scope
- **THEN** 系统拒绝执行数据库、搜索或文件访问
- **AND** 不回退到 local、进程上一次使用的 scope 或客户端自报值
