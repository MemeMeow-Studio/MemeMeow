## Purpose

为公共核心提供独立且 scope-safe 的合集 CRUD、详情和成员维护 HTTP 边界，在不改变合集身份、分页、成员或错误契约的前提下，让入口层只负责装配和路由声明。

## ADDED Requirements

### Requirement: 合集资源路由保持兼容

系统 MUST 继续注册合集列表、创建、详情、重命名、删除和成员维护的 canonical 路由，保持既有 HTTP method、status、tags、路径参数、分页 query 约束和旧 handler import；合集 ZIP 导入与导出不属于本边界。

#### Scenario: CRUD 和成员 route metadata 保持稳定

- **WHEN** 公共应用完成装配
- **THEN** `/collections` 仍注册单个 GET 和 201 POST，`/collections/{collection_id}` 仍注册 GET、PATCH、DELETE，成员入口仍注册 POST `/items` 和 DELETE `/items/{meme_id}`
- **AND** `/collections/import` 与 `/collections/{collection_id}/export` 不由新模块重复注册

### Requirement: 合集操作绑定可信 scope

合集 HTTP handler MUST 只通过入口注入的当前 scope environment 读取或修改合集及 Meme，客户端不得通过 query、JSON 或路径以外的范围字段覆盖 scope；跨 scope 或非法资源标识 MUST 投影为既有资源不存在错误。

#### Scenario: 请求不能选择其它 scope

- **WHEN** 列表或详情请求携带 `scope_id`、`user_id` 或其它未知 query 字段
- **THEN** handler 在进入 repository 前返回 `400/invalid_request`

#### Scenario: 跨 scope 合集不可见

- **WHEN** 当前 scope 使用另一 scope 的合集 ID 请求详情、重命名、删除或成员操作
- **THEN** 系统返回 `404/collection_not_found`，且不泄露合集或成员信息

### Requirement: CRUD 和详情响应保持稳定

合集 handler MUST 保持名称规范化、当前 scope 内名称唯一、稳定 UUID、成员数量、按加入顺序的封面和分页成员投影；详情成员 MUST 使用当前文件名、稳定 `meme_id`、受控 `/media/{meme_id}` 地址和现有 metadata 状态。

#### Scenario: 创建和重命名合集

- **WHEN** 使用合法名称创建或重命名合集
- **THEN** 成功响应返回规范化名称、稳定 `collection_id`、成员数量、封面和时间字段
- **AND** 重复名称返回 `409/collection_exists`，不修改已有合集

#### Scenario: 浏览合集详情

- **WHEN** 请求存在合集的一页详情
- **THEN** 响应返回合集摘要、成员总数、分页信息以及按加入时间和 `meme_id` 稳定排序的成员
- **AND** 每个成员仅包含公开文件信息、metadata 状态和受控媒体 URL

### Requirement: 成员维护保持原子和幂等

批量加入 MUST 在当前 scope 内原子校验全部 Meme，重复成员和请求内重复 ID MUST 作为成功处理；任一 Meme 不存在时整批 MUST 失败。单成员移除 MUST 保持幂等，并且任何成员操作都不得删除图片文件。

#### Scenario: 批量加入和重复加入

- **WHEN** 向存在的合集提交一组当前 scope Meme ID，其中包含已存在或重复 ID
- **THEN** 系统在一个事务内建立缺失关系并返回新增数、已存在数和最终成员数

#### Scenario: 批次包含无效 Meme

- **WHEN** 批量成员请求包含不存在或跨 scope 的 Meme ID
- **THEN** 系统返回 `404/meme_not_found` 或既有请求错误，且不写入该批次任何成员关系

### Requirement: HTTP 模块依赖保持单向

公共合集 HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；可变 scope environment、metadata service 和错误构造依赖 MUST 由入口 callback 注入，旧入口 handler MUST 继续可调用且不产生重复 canonical route。

#### Scenario: 静态依赖和兼容入口检查

- **WHEN** 对新模块执行静态 import 检查并读取应用 route 表
- **THEN** 新模块不存在入口反向依赖，旧 `api` handler 名称仍可导入，且每个 CRUD/成员 canonical route 只注册一次
