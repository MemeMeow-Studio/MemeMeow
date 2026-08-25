## Purpose

为运行中 Agent 提供经过 task claim、scope、attempt 和目标图片版本约束的内部反向图片检索
接口，防止客户端伪造任务事实、跨 scope 读取或借裁剪参数替换图片。

## ADDED Requirements

### Requirement: Reverse image callback route remains compatible

系统 MUST 继续注册 `POST /internal/reverse-image/search`，保持 `internal` tag、multipart
字段、status/error projection 和旧 handler import。

#### Scenario: Route and legacy handler remain available

- **WHEN** 应用模板完成装配
- **THEN** canonical route 使用 `POST`、`internal` tag，且 `api.internal_reverse_image_search`
  仍可导入并转发同一组表单参数。

### Requirement: Binding and target image are fail-closed

handler MUST 在读取 reverse-image service 前验证 callback binding、registration、task_id、
claim generation、attempt、token scope、持久任务和目标 Meme SHA；客户端不得提交 scope、
路径或执行选择字段覆盖这些事实。

#### Scenario: Forged or stale callback is rejected

- **WHEN** binding 缺失、任务不匹配、旧 claim、跨 scope 或上传 SHA 与目标不一致
- **THEN** 系统返回稳定的 `401/agent_callback_unauthorized` 或
  `401/agent_callback_invalid_execution`，且不调用 reverse-image service。
- **WHEN** callback registration 缺失或上传内容超过该 registration 的 body 上限
- **THEN** 系统返回既有的 `413/agent_callback_body_too_large`，且不调用 reverse-image service。

### Requirement: Request binding and controlled crop remain compatible

request id/header/digest MUST 继续按 callback binding 校验；`auto_crop` 只能在上传整图已与
目标 SHA 匹配后调用确定性服务端裁剪，检索参数和 source SHA 必须原样传给 service。

#### Scenario: Header and body ids cannot be rebound

- **WHEN** body `request_id` 与已验证 header request id 不同，或 digest 不合法
- **THEN** 系统返回 `401/agent_callback_invalid_execution`，且不调用 reverse-image service。

#### Scenario: Valid target is forwarded once

- **WHEN** binding、持久任务和上传目标图片均匹配
- **THEN** service 收到规范 `ReverseImageRequest`，包含目标 SHA、callback binding 和校验后的
  request id/digest，handler 返回 service 结果。

### Requirement: Reverse image callback module dependencies remain one-way

公共 callback HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；可变数据库、scope service、
registration 和错误依赖 MUST 通过 callback 注入。

#### Scenario: Dependency boundary is preserved

- **WHEN** 静态检查模块 import
- **THEN** 不包含入口反向导入，旧 handler 仍可调用且路由表不增加重复 callback route。
