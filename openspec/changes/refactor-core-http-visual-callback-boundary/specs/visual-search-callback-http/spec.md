## Purpose

为运行中 Agent 提供经过 task claim、scope、attempt 和幂等 callback fact 约束的内部视觉匹配接口，避免客户端伪造内部执行事实或跨范围读取结果。

## ADDED Requirements

### Requirement: Visual callback route remains compatible

系统 MUST 继续注册 `POST /internal/visual-search/match`，保持 `internal` tag、请求模型字段、status/error projection 和旧 handler/model import。

#### Scenario: Route and legacy names remain available

- **WHEN** 应用模板和宿主应用完成装配
- **THEN** canonical route 使用 `POST`、`internal` tag，且 `api.VisualMatchRequest` 与旧 handler 名称仍可导入

### Requirement: Binding and scope are fail-closed

handler MUST 在读取业务 service 前验证 callback binding、registration、task_id、claim generation、attempt、target SHA 和当前 scope task；客户端不得提交 scope、target 或 execution selector 覆盖这些事实。

#### Scenario: Missing or stale binding is rejected

- **WHEN** binding 缺失、task_id 不匹配、registration 缺失或持久 task 不符合 claim
- **THEN** 系统返回 `401/agent_callback_unauthorized` 或 `401/agent_callback_invalid_execution`，不调用视觉 service

### Requirement: Callback request id is bound and idempotent

request_id MUST 与 header、binding input digest 和持久 callback fact 绑定；已完成 fact 必须直接返回同一安全 result，未完成 fact 必须先提交 started 事实再调用视觉 service。

#### Scenario: Completed fact is replayed without a second service call

- **WHEN** 同一绑定和 request_id 的 callback fact 已 completed 且 result 是对象
- **THEN** 系统直接返回 fact result，不再次调用视觉 service

#### Scenario: Service failure finishes the fact

- **WHEN** started fact 后视觉 service 返回稳定 `VisualSearchError` 或数据库错误
- **THEN** 系统先将 fact 标记 failed，再返回既有错误 status/code

### Requirement: Visual callback module dependencies remain one-way

公共 callback HTTP 模块 MUST 不导入 `api.py` 或 `server_api`；scope/database/service/error 等可变应用依赖 MUST 通过 callback 注入。

#### Scenario: Dependency boundary is preserved

- **WHEN** 静态检查 callback HTTP 模块和旧 import
- **THEN** 模块不包含入口反向导入，旧 `VisualMatchRequest`/handler 仍可调用
