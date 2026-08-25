## Purpose

为按稳定图片身份执行重命名和删除的入口提供独立、scope-safe 的公共 HTTP 边界，在保留
文件安全、metadata 一致性和 operation policy 计量事实的同时缩小应用入口职责。

## ADDED Requirements

### Requirement: 图片变更路由保持兼容

系统 MUST 继续注册 `POST /images/rename` 和 `POST /images/delete`，保持既有 status、tags、
请求字段、响应字段、旧 handler import 和 route 顺序；上传及只读图片路由不得由该边界重复注册。

#### Scenario: route metadata 稳定

- **WHEN** 公共应用完成装配
- **THEN** `/images/rename` 和 `/images/delete` 各注册一次，method 均为 POST、tag 均包含
  `images`，且原请求模型继续拒绝未知字段
- **AND** `/images/upload`、`/images`、`/images/metadata` 和 `/media/{meme_id}` 的注册不被
  新模块接管或重复添加

### Requirement: 变更目标必须绑定当前 scope

图片重命名和删除 MUST 只接受稳定 `meme_id`，目标记录和文件 MUST 由当前 scope 的 metadata
service 派生；客户端提交空 ID、路径、目录或 scope/user 选择器时 MUST 返回既有稳定错误，不能
访问或修改其它 scope 资源。

#### Scenario: 缺少或伪造目标

- **WHEN** 请求缺少 `meme_id`，或 JSON/query/header 携带 scope、user、filename、directory
  等旧选择字段
- **THEN** 系统在 metadata 或 operation policy 之前返回 `400/meme_id_required` 或
  `400/invalid_request`，且不触碰文件和持久化服务

#### Scenario: 跨 scope 目标不可见

- **WHEN** 当前 scope 使用其它 scope 的 Meme ID 请求重命名或删除
- **THEN** 系统返回 `404/meme_not_found`，且不泄露目标路径、名称或其它 scope 信息

### Requirement: 重命名保持文件和 metadata 安全语义

重命名 MUST 从当前记录的原始扩展名派生目标文件名，拒绝路径分隔符、控制字符、越界存储
键和已存在的其它目标文件；成功时 MUST 原子更新文件与 metadata 的 storage key，并返回
稳定 `meme_id`、当前文件名和受控 media URL。metadata 失败 MUST 映射为既有稳定错误，不得
伪造成功响应。

#### Scenario: 合法重命名和扩展名兼容

- **WHEN** 当前 scope 对存在图片提交合法的新名称，名称未提供原扩展名或扩展名大小写不同
- **THEN** 系统沿用原图片扩展名完成重命名，返回 `200`、原 `meme_id`、规范化文件名和
  `/media/{meme_id}`
- **AND** 检索缓存失效只在 metadata 成功后触发

#### Scenario: 非法或冲突目标

- **WHEN** 新名称包含路径/控制字符、无法通过业务 storage key 校验，或目标文件已存在且
  不是当前源文件
- **THEN** 系统返回 `400/invalid_filename` 或 `409/file_exists`，且不调用 metadata rename
  和检索失效

### Requirement: 删除保持 operation policy 收束

删除 MUST 在真实 metadata 副作用前以当前 scope、稳定资源 ID、记录 revision 和固定来源
建立 operation grant；拒绝或不可用时 MUST fail-closed。metadata 删除成功后 MUST 尝试
commit，commit 失败不得把已完成删除伪装成未删除；明确知道未产生 durable 副作用的稳定
metadata 错误 MUST 尝试 release。成功响应 MUST 只返回稳定 ID 和 `deleted: true`。

#### Scenario: 删除成功和 commit 异常

- **WHEN** operation acquire 允许且 metadata 删除成功，随后 policy commit 抛出稳定异常
- **THEN** 系统仍返回 `200` 的删除成功事实，且不会 release 已完成的 grant
- **AND** 检索缓存失效发生在 metadata 删除之后

#### Scenario: policy 拒绝或删除前失败

- **WHEN** operation acquire 被拒绝，或 metadata 明确报告 `meme_not_found`、`file_not_found`、
  `target_exists` 或 `invalid_storage_key` 等未产生 durable 副作用的错误
- **THEN** acquire 拒绝按宿主 policy 错误投影返回，metadata 失败返回稳定 `500` 错误并尝试
  release，且不得报告删除成功

### Requirement: HTTP 模块依赖保持单向

公共图片变更 HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；可变 scope service、错误构造、
policy 生命周期和检索失效 MUST 通过入口 callback 注入，旧入口 handler MUST 继续可调用且
不产生重复 canonical route。

#### Scenario: 静态依赖和兼容入口检查

- **WHEN** 对新模块执行静态 import 检查并读取应用 route 表
- **THEN** 新模块不存在入口反向依赖，旧 `api.rename_image` 与 `api.delete_image` 名称仍可
  导入，且每个图片变更 canonical route 只注册一次
